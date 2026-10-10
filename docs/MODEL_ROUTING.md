# Routing modeli agentów

Converge nie zakłada, że jeden model jest najlepszy do wszystkich etapów autonomicznego developmentu.
Domyślna konfiguracja jest **quality-first**: role o innych celach dostają inne modele, a Builder i
review lanes są celowo rozdzielone między różne rodziny modeli.

To jest część architektury bezpieczeństwa jakościowego. Review wykonywane przez ten sam model, który
wytworzył implementację, ma większe ryzyko powtórzenia tych samych błędnych założeń. Model diversity
nie zastępuje deterministic quality gates, ale zwiększa wartość semantic review.

> **Najpierw wybieraj model do roli, nie rolę do modelu.** Szczegółowy kontrakt Scout/Planner/Builder/
> Correctness/Architecture/Security Reviewer, model przepływu danych, MCP/Skills boundaries, tabela
> cech modeli zastępczych i audyt izolacji są w [AGENT_ROLES_AND_DATA_FLOW.md](AGENT_ROLES_AND_DATA_FLOW.md).

Ważne rozróżnienie: nazwy pod `models.profiles` są **profilami konfiguracji modelu**, a nie osobnymi
agentami. Na przykład profil `planner` jest używany przez Plannera i Architecture Reviewera, natomiast
profil `reviewer` jest używany przez Correctness Reviewera. LangGraph pozostaje deterministycznym
orkiestratorem; nie dodajemy osobnego modelu LLM „managera”.

## Domyślny routing

| Rola | Domyślny model | Context | Dlaczego |
| --- | --- | ---: | --- |
| Repo Scout | `deepseek-v4.1-flash:cloud` | provider-managed | szybka, read-only mapa aktualnego base commit |
| Planner | `deepseek-v4.1-flash:cloud` | provider-managed | reasoning/tool use do analizy architektury i wyboru najmniejszego kolejnego kroku |
| Builder | `kimi-k2.7-code:cloud` | provider-managed | coding-focused long-horizon agent do wieloetapowego software engineering |
| Correctness Reviewer | `glm-5.3-flash:cloud` | provider-managed | niezależna rodzina od Buildera; zachowanie, edge cases, testy i compatibility |
| Architecture Reviewer | `deepseek-v4.1-flash:cloud` | provider-managed | świeża read-only sesja do dependency direction, boundaries i architectural drift |
| Security Reviewer | `deepseek-v4.1-flash:cloud` | provider-managed | niezależny od Buildera read-only security/trust-boundary review |

Architektura Reviewera jest teraz fan-outem, a nie pojedynczym wywołaniem. `workflow.review_roles`
definiuje jawne lane'y, które OpenCode uruchamia równolegle w świeżych procesach/sesjach nad tym samym
worktree. Wszystkie te role są read-only. Wyniki agreguje deterministyczny kod Converge: jeżeli choć
jeden lane zwróci `reject`, nie zwróci poprawnego JSON albo jego proces/model ulegnie awarii, wynik
zbiorczy jest `reject`.

Dzięki temu brak odpowiedzi Security Reviewera nie może zostać pomylony z brakiem problemów
bezpieczeństwa. Failure-to-review jest failure-to-integrate.

Aktualny payload katalogu OpenWebUI podaje dokładne ID modeli, ale nie publikuje limitów context/output.
Dlatego referencyjny preset pozostawia `context_tokens`/`output_tokens` jako `null` zamiast zgadywać.
Jawne limity należy dodać dopiero po ich wiarygodnym potwierdzeniu dla tego samego gateway/provider path.

## Jawny bounded retry i fallback

Każda rola może mieć `provider_retries` (0–3 dodatkowe próby primary modelu) oraz uporządkowane
`fallback_model_profiles` (maksymalnie cztery profile, każdy użyty raz). Referencyjny preset ustawia
`provider_retries: 1`: kolejne próby oddziela deterministyczny backoff (5–60 s), więc krótkie
przerwy transportowe są mostkowane w ramach jednego wywołania zamiast natychmiastowego
przełączania. Profil fallback może wskazać inny model w OpenWebUI albo innego istniejącego
providera OpenCode.

Failover nie zmienia roli ani polityki: nowe wywołanie ma świeżą sesję, identyczny system prompt,
permissions, write/read-only boundary i ten sam LangGraph state. Zmieniane są wyłącznie jawne
parametry profilu modelu. Converge ponownie liczy context budget; zbyt mały profil nie otrzyma cicho
uciętego authoritative core. Non-zero execution, timeout albo exception mogą uruchomić następną
próbę, lecz malformed JSON i semantic rejection pozostają normalnym wynikiem workflow.

Każda próba zapisuje role/model/profile/exit status do `<state_dir>/provider-health.jsonl` bez raw
output. Wybrany model i cała bounded lista prób trafiają również do context evidence fazy, więc
failover nie jest ukrytą zmianą policy.

Niepowodzenie całego wywołania klasyfikuje się strukturalnie (protokołowe `error` events OpenCode,
wyjątki executora) jako awaria transportowa lub procesowa — nigdy przez dopasowanie tekstu błędu.
Gdy wszystkie próby Plannera były transportowe, Graph wykorzystuje osobny ograniczony budżet
odzyskiwania providera (3 próby, backoff 30/60/120 s) bez zużywania semantycznych prób planowania;
wyczerpanie budżetu deterministycznie zatrzymuje run (awaria runtime) bez bramki HITL Plannera,
która pozostaje zarezerwowana dla powtarzających się semantycznych błędów kontraktu. Krótka
awaria providera nie pyta człowieka, dopóki budżet odzyskiwania nie jest wyczerpany.

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

Aktualny preset używa tylko modeli rzeczywiście widocznych w dostarczonym katalogu OpenWebUI:

| Profil | Model | Zastosowanie |
| --- | --- | --- |
| Builder fallback | `glm-5.3-flash:cloud` | zapasowy writer po execution/provider failure Kimi |
| Planner/reviewer fallback | `glm-5.3-flash:cloud` lub `deepseek-v4.1-flash:cloud` | świeża sesja innej aktywnej rodziny zgodnie z rolą |

`gemma4:31b-cloud` jest widoczny, ale jego rekord ma `capabilities: null`; nie trafia do obowiązkowej
lane bez benchmarku tool calling i schema adherence. `code-arena`, `code-arena-mid` i `math-arena`
są celowo wyłączone, bo arena ukrywa dokładną tożsamość modelu użytego w trwałych dowodach runu.

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

Aktualny referencyjny katalog zawiera stabilne modele cloud oraz arena IDs, ale nie zawiera
zweryfikowanego zestawu lokalnych modeli do wszystkich wymaganych ról. Dlatego
`examples/converge.yaml` wybiera `models.mode: cloud` i definiuje tylko zestaw `cloud`.

Converge nadal obsługuje `models.mode: local`. Lokalny zestaw należy dodać dopiero po sprawdzeniu
dokładnych ID przez `converge models` i benchmarku tool/schema behavior.

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
Przed zaakceptowaniem zamiany wykonaj dodatkowo mały benchmark na swoim repo: sprawdź schema/JSON
adherence, poprawność tool calls, liczbę zbędnych kroków, latency/koszt i jakość wyników konkretnej
roli. Szczegółowe cechy i wagi są w `AGENT_ROLES_AND_DATA_FLOW.md`.

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
- Generated-catalog wpis `limit` jest emitowany tylko wtedy, gdy znane są OBA limit
  (`context` i `output`), ponieważ OpenCode odrzuca konfigurację z niepełnym `limit`
  (`Missing key ...limit.output`). Częściowo znany limit jest pomijany w całości,
  a sprzeczne jawne limity dla tego samego modelu pozostają błędem konfiguracji.

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

- Kimi K2.7 Code: current gateway ID `kimi-k2.7-code:cloud`
- GLM 5.3 Flash: current gateway ID `glm-5.3-flash:cloud`
- DeepSeek V4.1 Flash: current gateway ID `deepseek-v4.1-flash:cloud`
- Gemma4 31B: current gateway ID `gemma4:31b-cloud` (not in mandatory lanes until capabilities are verified)
