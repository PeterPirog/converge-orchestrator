# Routing modeli agentów

Converge nie zakłada, że jeden model jest najlepszy do wszystkich etapów autonomicznego developmentu.
Domyślna konfiguracja jest **quality-first**: role o innych celach dostają inne modele, a Builder i
review lanes są celowo rozdzielone między różne rodziny modeli.

To jest część architektury bezpieczeństwa jakościowego. Review wykonywane przez ten sam model, który
wytworzył implementację, ma większe ryzyko powtórzenia tych samych błędnych założeń. Model diversity
nie zastępuje deterministic quality gates, ale zwiększa wartość semantic review.

## Domyślny routing

| Rola | Domyślny model | Context | Domyślny fallback |
| --- | --- | ---: | --- |
| Repo Scout | `deepseek-v4-flash:cloud` | 1,048,576 | `glm-5.3-flash:cloud` |
| Planner | `deepseek-v4-pro:cloud` | 1,048,576 | `glm-5.3-flash:cloud` |
| Builder | `kimi-k2.7-code:cloud` | 262,144 | **brak** |
| Correctness Reviewer | `glm-5.3-flash:cloud` | 1,048,576 | `deepseek-v4-pro:cloud` |
| Architecture Reviewer | `deepseek-v4-pro:cloud` | 1,048,576 | `glm-5.3-flash:cloud` |
| Security Reviewer | `gpt-oss:120b` | 131,072 | `glm-5.3-flash:cloud` |

Scout wykonuje szybkie read-only mapowanie dokładnego base commit przed Plannerem. Planner dostaje
następnie deterministycznie wybrany target requirement i planuje tylko minimalny krok dla tego celu.
Builder pozostaje jedynym writerem.

Architektura Reviewera jest fan-outem, a nie pojedynczym wywołaniem. `workflow.review_roles` definiuje
jawne lane'y, które OpenCode uruchamia równolegle w świeżych procesach/sesjach nad tym samym worktree.
Wszystkie te role są read-only. Wyniki agreguje deterministyczny kod Converge: jeżeli choć jeden lane
zwróci `reject`, nie zwróci poprawnego JSON albo jego proces/model ulegnie awarii po wyczerpaniu
fallbacków, wynik zbiorczy jest `reject`.

Dzięki temu brak odpowiedzi Security Reviewera nie może zostać pomylony z brakiem problemów
bezpieczeństwa. Failure-to-review jest failure-to-integrate.

## Bounded model fallback

`fallback_model_profiles` jest mechanizmem dostępności dla **read-only** ról. Nie zmienia LangGraph,
Task Envelope, target requirement ani promptu. Jest to jawny, skończony łańcuch modeli skonfigurowany
w tym samym `converge.yaml`.

Przykład:

```yaml
agents:
  planner:
    agent: converge-planner
    model_profile: planner
    fallback_model_profiles: [reviewer]
    timeout_seconds: 1800
```

Zasady są fail-closed:

- maksymalnie dwa profile fallback;
- każdy profil musi istnieć w `models.profiles`;
- primary i fallback muszą wskazywać różne skonfigurowane modele;
- każdy model w łańcuchu musi mieć jawne `context_tokens`;
- Builder nie może mieć `fallback_model_profiles`;
- retry następuje dopiero po wyjątku wykonania albo non-zero exit OpenCode;
- successful primary call nie uruchamia żadnego dodatkowego modelu;
- semantic failure przy poprawnym procesie nie jest maskowany przez failover: malformed plan przechodzi
  przez bounded Planner repair, a review `reject` przez zwykły repair/replan loop;
- każda próba jest świeżą sesją OpenCode i używa **dokładnie tego samego wyrenderowanego promptu**;
- continuity pochodzi wyłącznie z LangGraph state/evidence, nie z historii sesji modelu.

Przed pierwszą próbą Converge liczy input budget względem **najmniejszego context window** oraz
**największego output reserve** w całym łańcuchu. Dzięki temu fallback nigdy nie wymusza późniejszego
cichego obcięcia immutable requirements, Task Envelope ani review diffu.

Każda próba trafia do istniejącego `context-usage.jsonl` jako bounded metadata: profil, resolved model,
variant, numer próby, outcome, return code albo typ wyjątku. Odpowiedź modelu i sekrety nie są kopiowane
do tego evidence.

### Dlaczego Builder nie ma fallbacku

Builder jest jedynym writerem worktree. Nieudany proces może pozostawić częściowo zmienione pliki.
Automatyczne uruchomienie innego modelu nad takim stanem bez deterministycznego rollbacku mogłoby
zmieszać dwa niezależne zamiary implementacyjne i zwiększyć dryft celu. Dlatego konfiguracja odrzuca
Builder fallback już podczas walidacji.

Writer failover może zostać dodany dopiero razem z osobnym mechanizmem snapshot/rollback worktree,
który dowodzi, że druga próba zaczyna od identycznego stanu wejściowego.

### Model fallback a gateway failover

Referencyjne profile przechodzą przez ten sam OpenWebUI gateway. Obecny fallback pomaga wtedy, gdy
konkretny model/backend jest niedostępny lub OpenCode kończy próbę błędem, ale **nie jest redundancją
samego OpenWebUI**. Awaria całego gatewaya nadal wykorzysta bounded retry/recovery i może ostatecznie
prowadzić do exception-based HITL.

Pełny provider/gateway failover wymaga jawnie skonfigurowanych niezależnych providerów oraz osobnej
polityki health/routing. Converge nie wybiera automatycznie przypadkowego modelu z katalogu.

## Limity kontekstu

Dokładne limity kontekstu są zapisane w `examples/converge.yaml` jako `context_tokens`. Converge
przekłada je na stable OpenCode `provider.models.<id>.limit.context`, dzięki czemu OpenCode może
zarządzać compaction względem rzeczywistego limitu custom gateway.

Domyślne profile pozostawiają `request_body: {}`. Jest to świadome: modele reasoning/coding mają
provider-specific ustawienia i ich optymalnych parametrów nie należy zgadywać w uniwersalnym
orkiestratorze. Converge uzyskuje powtarzalność przez immutable requirements, Task Envelope,
deterministic gates, compliance, niezależny review fan-out i CI, a nie przez wymuszanie jednego
`temperature` dla każdego modelu.

## Dlaczego trzy review lanes

Jedna ogólna recenzja miesza konkurujące cele i łatwo pomija część powierzchni błędów. Referencyjny
preset rozdziela je następująco:

- **Correctness** — observable behavior, edge cases, test adequacy, backward compatibility;
- **Architecture** — Source of Truth, boundaries, coupling/cohesion, dependency direction, scope;
- **Security** — authn/authz, secrets, injection, path/command handling, trust boundaries i insecure
  defaults.

Każdy lane dostaje ten sam rzeczywisty diff i ten sam Task Envelope, ale inną instrukcję systemową.
Żaden z nich nie może edytować worktree, delegować nested task ani wywoływać arbitralnego shella.
Builder pozostaje jedynym writerem.

`ReviewResult` zachowuje mapę lane -> verdict oraz przypisuje każde finding do konkretnego reviewera.
Builder dostaje ten agregat w repair loop, więc może naprawić wszystkie blocking findings w jednej
kolejnej iteracji.

## Modele alternatywne

Z dostępnego katalogu przydatne są również:

| Profil | Model | Context | Zastosowanie |
| --- | --- | ---: | --- |
| Local long-horizon | `laguna-s-2.1:latest` | 262,144 | lokalne długie zadania; bardzo duże wymagania pamięciowe |
| Coding alternative | `qwen3-coder-next:cloud` | 262,144 | coding/tool use jako alternatywa implementacyjna |

Nie są one domyślnymi fallbackami read-only, ponieważ ich 262k context zmniejszyłby fail-closed budget
Plannera/Scouta z 1M nawet wtedy, gdy primary działa poprawnie. W razie świadomej zmiany chain zawsze
sprawdź wpływ najmniejszego `context_tokens`.

## Dlaczego nie jeden model wszędzie

Domyślnie nie konfigurujemy `kimi-k2.7-code:cloud` jako Planner, Builder i wszystkie review lanes.
Model codingowy jest właściwym wyborem dla Writer loop, ale architektoniczny Planner powinien bardziej
optymalizować kolejność i zakres zmian, a reviewerzy mają przede wszystkim szukać błędów i regresji.
Dodatkowo niezależność rodzin zmniejsza ryzyko skorelowanego self-review.

Architecture Reviewer może używać tej samej rodziny co Planner, ponieważ nie ocenia własnej
implementacji, działa w świeżej sesji i ma inną funkcję decyzyjną. Krytyczna separacja to przede
wszystkim Writer vs Reviewer oraz dodatkowa niezależność Security Reviewera.

Podobnie model o największym context window nie jest automatycznie najlepszym Builderem. Converge
przekazuje Builderowi minimalny kontekst zadania i dokładne target requirement statements; duży context
jest szczególnie wartościowy dla Plannera i reviewerów analizujących szeroki repo/architecture context.

## Lokalność vs jakość

Referencyjny preset jest quality-first i używa modeli cloud tam, gdzie ich specjalizacja jest
najbardziej użyteczna. Jeżeli polityka projektu wymaga local-only, zachowaj role i review fan-out, ale
zmień profile na jawnie wybrane modele lokalne z prawidłowymi limitami kontekstu.

Nie zaleca się redukowania trzech lane'ów do jednego wyłącznie dlatego, że projekt działa local-only.
Jeżeli sprzęt nie mieści kilku ciężkich modeli jednocześnie, ustaw `max_parallel_reviews: 1` lub `2`.
Semantyka pozostaje ta sama — zmienia się tylko concurrency, nie wymaganie przejścia wszystkich lane'ów.

## Konfiguracja review fan-out

```yaml
workflow:
  review_roles:
    - correctness_reviewer
    - architecture_reviewer
    - security_reviewer
  max_parallel_reviews: 3
```

`review_roles` musi wskazywać unikalne, skonfigurowane role review. Converge odrzuca próbę użycia
Buildera/Plannera jako review lane oraz duplikaty OpenCode agent IDs. Starsza konfiguracja, która nie ma
`review_roles`, zachowuje wcześniejsze zachowanie i wywołuje pojedynczą rolę `reviewer`.

## Jak zmienić model

Najpierw zobacz dokładne ID widoczne przez skonfigurowany OpenWebUI:

```bash
converge models --config /workspace/my-project/converge.yaml
```

Następnie zmień `models.profiles.<role>.model`, `context_tokens` oraz — jeśli potrzebujesz —
`agents.<role>.fallback_model_profiles`. Nie edytuj generowanego `<state_dir>/opencode.generated.json`.

Po zmianie zawsze uruchom:

```bash
converge doctor --config /workspace/my-project/converge.yaml
```

`doctor` sprawdza modele widoczne przez gateway, a walidacja konfiguracji sprawdza spójność fallback
chain przed uruchomieniem workflow.

## Parametry profilu

Profile obsługują:

```yaml
models:
  profiles:
    planner:
      model: deepseek-v4-pro:cloud
      context_tokens: 1048576
      output_tokens: null
      request_body: {}
```

- `context_tokens` trafia do OpenCode `limit.context` oraz fail-closed budget Converge.
- `output_tokens`, jeżeli jest znane i jawnie ustawione, trafia do `limit.output`.
- `null` oznacza: nie zgaduj; pozostaw zarządzanie providerowi/OpenCode.
- `request_body` służy świadomym provider-specific override'om.

Jeżeli kilka profili wskazuje ten sam model przez ten sam gateway, sprzeczne jawne limity są błędem
konfiguracji zamiast cichego wyboru jednej wartości. Fallback chain dodatkowo nie może powtarzać tego
samego skonfigurowanego celu modelowego.

## Kryteria doboru modelu dla nowego projektu

Przy zmianie katalogu modeli oceniaj przede wszystkim:

1. **Builder:** coding + tool use + stabilność długiego tool loop.
2. **Planner:** reasoning, instruction following i zdolność pracy na szerokim kontekście.
3. **Correctness Reviewer:** silne coding/reasoning i inna rodzina niż Builder.
4. **Architecture Reviewer:** szeroki kontekst, instruction following i analiza zależności/boundaries.
5. **Security Reviewer:** niezależność modelu, dobra analiza kodu i ostrożne tool interpretation.
6. **Scout:** szybkość i koszt, ale nadal poprawne tool calling.
7. **Fallback:** model o wystarczającym kontekście, innej rodzinie i akceptowalnym koszcie awaryjnym.

Nie wybieraj modelu wyłącznie na podstawie liczby parametrów albo długości context window.

## Referencje modeli użytych przez preset

- Kimi K2.7 Code: https://ollama.com/library/kimi-k2.7-code
- DeepSeek V4 Pro: https://ollama.com/library/deepseek-v4-pro
- GLM 5.3 Flash: https://ollama.com/library/glm-5.3-flash
- Laguna S 2.1: https://ollama.com/library/laguna-s-2.1
- gpt-oss: https://openai.com/open-models/
