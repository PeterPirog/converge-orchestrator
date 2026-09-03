# Audyt zbieżności z referencyjną architekturą autonomicznych agentów

Ten dokument śledzi zgodność Converge z referencyjnymi założeniami autonomicznego workflow
software-engineering: niezmienny plik architektury, wyspecjalizowane role, małe iteracje, testy,
niezależny review, Git/GitHub, MCP, minimalny HITL i prostą rekonfigurację między projektami.

Audyt rozróżnia **cel architektoniczny** od konkretnej technologii opisanej w materiale referencyjnym.
Jeżeli nowsza wersja platformy zastąpiła wskazany mechanizm, Converge zachowuje cel, ale nie utrzymuje
legacy dependency wyłącznie dla literalnej zgodności.

## Stan ogólny

Aktualnie 11 z 15 obszarów jest zgodnych albo zaimplementowanych mocniej niż w referencji, a 4 są
częściowe. Nie ma już funkcjonalnej luki wymagającej osobnej kategorii technologicznego odejścia:
OpenWebUI pełni rolę operatorskiego punktu wejścia przez wspierany Workspace Tool, podczas gdy
LangGraph pozostaje trwałym silnikiem procesu.

Największe pozostałe luki operacyjne to sandbox na poziomie OS/container, zarządzanie długim kontekstem
i bardziej rygorystyczne wymuszanie TDD. Nie ma obecnie luki, która wymagałaby osłabienia immutable
Source of Truth, deterministic policy albo zasady one-writer-per-worktree.

## Macierz zbieżności

| Obszar | Status | Implementacja Converge | Pozostała luka |
| --- | --- | --- | --- |
| Niezmienny Markdown jako Source of Truth | **STRONGER** | plik poza repo, OS read-only, SHA-256 pin, ponowne sprawdzanie przed zmianami i bezpośrednio przed integration | brak krytycznej luki |
| Przeciwdziałanie architectural drift | **STRONGER** | traceable `contract.json`, source anchors, exact target requirement injection, compliance i monotonic verifier policy | semantic-only requirements nadal wymagają LLM review |
| Deterministyczny kontroler nad LLM | **ALIGNED** | LangGraph + Pydantic state/policy; LLM nie steruje merge ani nie może anulować failing gate | brak krytycznej luki |
| Planner / Worker / Reviewer | **STRONGER** | Scout RO mapuje dokładny base commit, Planner RO wybiera task, Builder jest jedynym writerem, trzy niezależne review lanes są RO | dalsze specialty analyzers są opcjonalnym rozszerzeniem |
| Autonomiczny TDD / repair loop | **PARTIAL** | Builder ma obowiązek testów, deterministic quality gates i bounded repair/replan | brak deterministycznego wymogu red-before-green dla zmian, gdzie TDD jest możliwe |
| Izolacja Git | **STRONGER** | osobny `git worktree` per task zamiast przełączania/stash/reset w głównym checkout | cleanup po crash wymaga dalszego hardeningu |
| Code review jako bariera przed dryfem | **STRONGER** | correctness + architecture + security wykonywane równolegle; jeden reject albo reviewer failure blokuje integration | future specialty lanes mogą zostać dodane później |
| GitHub PR + CI | **ALIGNED** | deterministic integrator, branch push, PR, bounded CI wait, opcjonalny merge po PASS | required-check/branch-protection discovery jeszcze niepełne |
| MCP jako szyna narzędziowa | **PARTIAL** | neutralna konfiguracja MCP w `converge.yaml`, generowana do stable OpenCode | Converge nie wymusza konkretnego katalogu git/github/pytest/desktop MCP; część funkcji realizuje bezpieczniej deterministycznym kodem lokalnym |
| OpenWebUI jako punkt wejścia operatora | **ALIGNED** | natywny Workspace Tool nad Bearer-authenticated FastAPI; read-only status/compliance/evidence oraz confirmation-gated register/bootstrap/start/pause/resume/decision; durable state pozostaje w LangGraph | docelowy dashboard może poprawić ergonomię, ale nie jest wymagany do kontroli workflow |
| Łatwa rekonfiguracja projektu | **ALIGNED** | jeden `converge.yaml`, ścieżki względne do YAML, profiles/models/MCP/quality/workflow oraz Valves dla operator bridge | pełny GUI editor projektu pozostaje opcjonalny |
| Minimalny HITL | **STRONGER** | przerwanie tylko dla risk policy lub wyczerpania bounded recovery; człowiek nie może zatwierdzić failing deterministic gate | polityka klasyfikacji ryzyka wymaga dalszego rozszerzenia |
| Least privilege / sandbox | **PARTIAL** | role OpenCode mają deny-by-default; Builder nie może push/gh/reset/clean/external directory | permission model nie jest kernel boundary; potrzebny container/OS sandbox |
| Dual-memory / context rotation | **PARTIAL** | małe Task Envelopes, fresh review sessions, bounded Scout snapshot, jawne context limits, brak chat history jako durable state | brak automatycznego token-budget monitoringu, session rotation i kompresora working memory |
| Evidence + compliance | **STRONGER** | SQLite checkpoints, evidence bundles, events, compliance snapshot, requirement verifiers, baseline/candidate regression policy | docelowo metrics/tracing i storage dla multi-worker |

## Wspierana implementacja zamiast legacy OpenWebUI Pipelines

Materiał referencyjny zakłada OpenWebUI Pipelines/Manifold jako główny silnik orkiestracji. Dla nowego
wdrożenia nie jest to już właściwy target. Aktualna dokumentacja OpenWebUI oznacza Pipelines jako
legacy i wskazuje wspierane mechanizmy Tools, Functions oraz zewnętrzne MCP/OpenAPI.

Converge realizuje funkcjonalny cel OpenWebUI w następującym układzie:

```text
OpenWebUI Workspace Tool
  |       |
  |       +--> native confirmation for mutations
  |
  +--> authenticated FastAPI control requests
              |
              v
        Converge FastAPI
              |
              v
       LangGraph durable workflow
          |              |
       OpenCode        GitHub/CI
          |
        MCP/tools
```

FastAPI może wymagać `CONVERGE_API_TOKEN`; poza `/health` żądania wymagają Bearer auth. Workspace Tool
ma password-masked Valve na token i prosi operatora o natywne potwierdzenie przed register/bootstrap,
start, pause, resume oraz decyzją HITL. Brak potwierdzenia, odmowa, disconnect lub event-call error
kończy operację bez mutującego requestu.

To zachowuje pojedynczy wygodny punkt wejścia w OpenWebUI, ale stan, checkpointy, retry policy, repair,
compliance i dowody pozostają w serwisie/LangGraph zaprojektowanym do długotrwałego procesu. Chat nie
jest durable state.

## Repo Scout/Triage

Aktywny LangGraph dodaje Scouta bezpośrednio przed Plannerem:

```text
guard_plan -> pause_plan -> scout -> plan -> prepare_worktree
```

Scout jest read-only i działa na świeżo odświeżonym canonical base. Zapisuje dokładny SHA base commit,
krótką mapę stosu i ważnych ścieżek, uwagi o granicach architektury, ryzykowne powierzchnie oraz
wskazówki requirement-ID -> code paths. Wynik jest ograniczany rozmiarem i utrwalany jako
`baseline.repo_scout` oraz `evidence/<run>/run/repo-scout.json`.

Snapshot nie staje się nowym Source of Truth. Planner dostaje go jako advisory context i nadal może
weryfikować szczegóły w repo. Nieznane requirement IDs są odrzucane. Brak konfiguracji Scouta, błąd
modelu albo malformed JSON daje jawny fallback i nie zatrzymuje autonomicznego planowania.

## Parallel Review Coordinator

Aktywny preset używa trzech lane'ów:

```text
                        +--> correctness_reviewer (GLM 5.3 Flash)
quality gates -> fanout +--> architecture_reviewer (DeepSeek V4 Pro)
                        +--> security_reviewer (gpt-oss 120B)
                                      |
                                      v
                            deterministic aggregate
                                      |
                         all PASS -----+----- any REJECT/failure
                            |                       |
                       integrate                 repair/replan
```

Współbieżność jest tylko read-only. Nie wprowadza wielowriterowego dostępu do worktree. Wynik każdego
lane'a jest niezależny, a failure-to-review jest traktowane jako rejection, nie jako PASS.

## Priorytety dalszej zbieżności

Kolejność prac powinna maksymalizować autonomię bez zwiększania blast radius:

1. **Context budget + session rotation** — limity tokenów, świeże sesje i jawne summary artifacts zamiast rosnącej historii.
2. **Sandbox runner** — filesystem/network/process policy poniżej poziomu permissions OpenCode.
3. **TDD evidence policy** — opcjonalny verifier red-before-green dla tasków modyfikujących zachowanie.
4. **Risk classifier + compatibility adapters** — public API, migracje danych, sekrety i auth jako deterministyczne/przedintegracyjne sygnały ryzyka.
5. **Crash/chaos hardening** — leases, stale worktree cleanup, killed-process recovery i długie CI.

## Kryterium docelowe

Projekt jest uznany za operacyjnie zbieżny z referencyjną wizją, gdy:

- immutable requirements nie mogą zostać zmienione ani wyparte przez derived summary;
- każde zadanie jest bounded i ma jednego writera;
- deterministyczne testy/policy oraz wszystkie wymagane review lanes przechodzą przed integracją;
- GitHub CI potwierdza candidate commit;
- system potrafi sam naprawiać/replanować do limitu i dopiero wtedy eskalować;
- operator może uruchamiać i kontrolować workflow z OpenWebUI bez przechowywania durable state w chacie;
- execution sandbox ogranicza szkody niezależnie od zachowania modelu;
- context rotation pozwala na długie projekty bez kumulowania nieograniczonej historii promptów.
