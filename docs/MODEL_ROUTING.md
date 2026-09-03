# Routing modeli agentów

Converge nie zakłada, że jeden model jest najlepszy do wszystkich etapów autonomicznego developmentu.
Domyślna konfiguracja jest **quality-first**: role o innych celach dostają inne modele, a Builder i
review lanes są celowo rozdzielone między różne rodziny modeli.

To jest część architektury bezpieczeństwa jakościowego. Review wykonywane przez ten sam model, który
wytworzył implementację, ma większe ryzyko powtórzenia tych samych błędnych założeń. Model diversity
nie zastępuje deterministic quality gates, ale zwiększa wartość semantic review.

## Domyślny routing

| Rola | Domyślny model | Context | Dlaczego |
| --- | --- | ---: | --- |
| Repo Scout | `deepseek-v4-flash:cloud` | 1,048,576 | szybka, read-only mapa aktualnego base commit |
| Planner | `deepseek-v4-pro:cloud` | 1,048,576 | frontier reasoning + tools/thinking; analiza architektury i wybór najmniejszego kolejnego kroku |
| Builder | `kimi-k2.7-code:cloud` | 262,144 | coding-focused long-horizon agent do wieloetapowego software engineering |
| Correctness Reviewer | `glm-5.3-flash:cloud` | 1,048,576 | niezależna rodzina od Buildera; zachowanie, edge cases, testy i compatibility |
| Architecture Reviewer | `deepseek-v4-pro:cloud` | 1,048,576 | szeroki kontekst i reasoning do dependency direction, boundaries i architectural drift |
| Security Reviewer | `gpt-oss:120b` | 131,072 | niezależna lokalna rodzina reasoning/tool-use do security i trust boundaries |

Architektura Reviewera jest teraz fan-outem, a nie pojedynczym wywołaniem. `workflow.review_roles`
definiuje jawne lane'y, które OpenCode uruchamia równolegle w świeżych procesach/sesjach nad tym samym
worktree. Wszystkie te role są read-only. Wyniki agreguje deterministyczny kod Converge: jeżeli choć
jeden lane zwróci `reject`, nie zwróci poprawnego JSON albo jego proces/model ulegnie awarii, wynik
zbiorczy jest `reject`.

Dzięki temu brak odpowiedzi Security Reviewera nie może zostać pomylony z brakiem problemów
bezpieczeństwa. Failure-to-review jest failure-to-integrate.

Dokładne limity kontekstu są zapisane w `examples/converge.yaml` jako `context_tokens`. Converge
przekłada je na stable OpenCode `provider.models.<id>.limit.context`, dzięki czemu OpenCode może
zarządzać compaction względem rzeczywistego limitu custom gateway.

## Jawny bounded retry i fallback

Każda rola może mieć `provider_retries` (0–3 dodatkowe próby primary modelu) oraz uporządkowane
`fallback_model_profiles` (maksymalnie cztery profile, każdy użyty raz). Referencyjny preset ustawia
`provider_retries: 0`, aby nie czekać ponownie na niedostępny model, i przechodzi bezpośrednio do
jawnego fallbacku. Profil fallback może wskazać inny model w OpenWebUI albo innego istniejącego
providera OpenCode.

Failover nie zmienia roli ani polityki: nowe wywołanie ma świeżą sesję, identyczny system prompt,
permissions, write/read-only boundary i ten sam LangGraph state. Zmieniane są wyłącznie jawne
parametry profilu modelu. Converge ponownie liczy context budget; zbyt mały profil nie otrzyma cicho
uciętego authoritative core. Non-zero execution, timeout albo exception mogą uruchomić następną
próbę, lecz malformed JSON i semantic rejection pozostają normalnym wynikiem workflow.

Każda próba zapisuje role/model/profile/exit status do `<state_dir>/provider-health.jsonl` bez raw
output. Wybrany model i cała bounded lista prób trafiają również do context evidence fazy, więc
failover nie jest ukrytą zmianą policy.

Domyślne profile pozostawiają `request_body: {}`. Jest to świadome: modele reasoning/coding mają
provider-specific ustawienia i ich optymalnych parametrów nie należy zgadywać w uniwersalnym
orkiestratorze. Converge uzyskuje powtarzalność przez Task Envelope, deterministic gates, compliance,
niezależny review fan-out i CI, a nie przez wymuszanie jednego `temperature` dla każdego modelu.

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

## Profile zapasowe

Po aktywacji parallel review kolejne przydatne role/model policies są następujące:

| Profil | Model | Context | Zastosowanie |
| --- | --- | ---: | --- |
| Local long-horizon fallback | `laguna-s-2.1:latest` | 262,144 | agentic coding i długie zadania bez zależności od cloud; bardzo duże wymagania pamięciowe |
| Coding fallback | `qwen3-coder-next:cloud` | 262,144 | coding/tool use jako zapasowy model implementacyjny |

Repo Scout i bounded model failover są aktywne bez dodawania nowych przejść LangGraph. Nie należy
dodawać nowych ról do `workflow.review_roles`, jeśli rola nie jest jednym z jawnie obsługiwanych
reviewerów.

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
zmień profile, np. Planner/Architecture/Builder na lokalny long-horizon model, a Security Reviewer na
`gpt-oss:120b`.

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

Następnie zmień `models.profiles.<role>.model` i, jeżeli znasz wartość z katalogu providera,
`context_tokens`. Nie edytuj generowanego `<state_dir>/opencode.generated.json`.

Po zmianie zawsze uruchom:

```bash
converge doctor --config /workspace/my-project/converge.yaml
```

`doctor` sprawdza, czy wszystkie modele aktywnych agentów są faktycznie widoczne przez gateway.

## Limity modelu

Profile obsługują:

```yaml
models:
  profiles:
    builder:
      model: kimi-k2.7-code:cloud
      context_tokens: 262144
      output_tokens: null
```

- `context_tokens` trafia do OpenCode `limit.context`.
- `output_tokens`, jeżeli jest znane i jawnie ustawione, trafia do `limit.output`.
- `null` oznacza: nie zgaduj; pozostaw zarządzanie providerowi/OpenCode.

Jeżeli kilka profili wskazuje ten sam model przez ten sam gateway, sprzeczne jawne limity są błędem
konfiguracji zamiast cichego wyboru jednej wartości.

## Parametry requestu

`request_body` jest dostępny dla świadomych, provider-specific override'ów:

```yaml
models:
  profiles:
    builder:
      model: kimi-k2.7-code:cloud
      request_body: {}
```

Pusty obiekt jest zalecanym punktem startowym. Parametry sampling/reasoning ustawiaj dopiero po
benchmarku na konkretnym repo i przez dokładnie ten sam OpenWebUI/OpenCode transport. Zmiana parametrów
nie może wpływać na integracyjne reguły bezpieczeństwa: testy, mandatory regression gate, independent
review i CI są nadrzędne.

## Kryteria doboru modelu dla nowego projektu

Przy zmianie katalogu modeli oceniaj przede wszystkim:

1. **Builder:** coding + tool use + stabilność długiego tool loop.
2. **Planner:** reasoning, instruction following i zdolność pracy na szerokim kontekście.
3. **Correctness Reviewer:** silne coding/reasoning i inna rodzina niż Builder.
4. **Architecture Reviewer:** szeroki kontekst, instruction following i analiza zależności/boundaries.
5. **Security Reviewer:** niezależność modelu, dobra analiza kodu i ostrożne tool interpretation.
6. **Scout:** szybkość i koszt, ale nadal poprawne tool calling.

Nie wybieraj modelu wyłącznie na podstawie liczby parametrów albo długości context window.

## Referencje modeli użytych przez preset

- Kimi K2.7 Code: https://ollama.com/library/kimi-k2.7-code
- DeepSeek V4 Pro: https://ollama.com/library/deepseek-v4-pro
- GLM 5.3 Flash: https://ollama.com/library/glm-5.3-flash
- Laguna S 2.1: https://ollama.com/library/laguna-s-2.1
- gpt-oss: https://openai.com/open-models/
