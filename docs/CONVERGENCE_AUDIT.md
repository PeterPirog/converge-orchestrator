# Audyt zbieżności z referencyjną architekturą autonomicznych agentów

Ten dokument śledzi zgodność Converge z referencyjnymi założeniami autonomicznego workflow
software-engineering: niezmienny plik architektury, wyspecjalizowane role, małe iteracje, testy,
niezależny review, Git/GitHub, MCP, minimalny HITL i prostą rekonfigurację między projektami.

Audyt rozróżnia **cel architektoniczny** od konkretnej technologii opisanej w materiale referencyjnym.
Jeżeli nowsza wersja platformy zastąpiła wskazany mechanizm, Converge zachowuje cel, ale nie utrzymuje
legacy dependency wyłącznie dla literalnej zgodności.

## Stan ogólny

Aktualnie 14 z 15 obszarów jest zgodnych albo zaimplementowanych mocniej niż w referencji, a 1 jest
częściowy. OpenWebUI pełni rolę operatorskiego punktu wejścia przez wspierany Workspace Tool, podczas
gdy LangGraph pozostaje trwałym silnikiem procesu. Długotrwała ciągłość pracy także nie zależy już od
ukrytej historii modelu: każdy agent call jest świeżą sesją, a continuity pochodzi z LangGraph state i
jawnych artefaktów evidence.

Deterministyczna klasyfikacja finalnego diffu dla sekretów, migracji, publicznego Python API oraz
auth/authz nie jest już luką. Największą pozostałą luką operacyjną jest crash/chaos recovery: leases,
stale worktree cleanup, wznowienie po zabitym procesie oraz długotrwałe oczekiwanie na CI. Dalszego
rozszerzenia wymagają także cross-language compatibility adapters, remote branch-protection awareness
i observability. OS/container execution boundary oraz wymuszane red-before-green dla behavior tasks
nie są już lukami architektonicznymi.

## Macierz zbieżności

| Obszar | Status | Implementacja Converge | Pozostała luka |
| --- | --- | --- | --- |
| Niezmienny Markdown jako Source of Truth | **STRONGER** | plik poza repo, OS read-only, SHA-256 pin, ponowne sprawdzanie przed zmianami i bezpośrednio przed integration | brak krytycznej luki |
| Przeciwdziałanie architectural drift | **STRONGER** | traceable `contract.json`, source anchors, exact target requirement injection, compliance i monotonic verifier policy; pełny contract nie jest już cicho obcinany do 80 wymagań | semantic-only requirements nadal wymagają LLM review |
| Deterministyczny kontroler nad LLM | **STRONGER** | LangGraph + Pydantic state/policy; LLM nie steruje merge, nie może anulować failing gate ani sam wyprodukować hard-block evidence bez deterministycznego klasyfikatora | brak krytycznej luki |
| Planner / Worker / Reviewer | **STRONGER** | Scout RO mapuje dokładny base commit, Planner RO wybiera task, Builder jest jedynym writerem, trzy niezależne review lanes są RO | dalsze specialty analyzers są opcjonalnym rozszerzeniem |
| Autonomiczny TDD / repair loop | **ALIGNED** | behavior task wymaga strukturalnego TDD contract; orkiestrator wykonuje baseline, test-only RED, literalny novel failure marker, deterministic test-artifact check, SHA-256 freeze i GREEN na tym samym gate; repair/replan są bounded, a HITL nie może ominąć RED | klasyfikacja `change_kind` pozostaje semantic/reviewer-backed i może być dalej wzmacniana regułami językowymi |
| Izolacja Git | **STRONGER** | osobny `git worktree` per task; w container sandbox shared `.git` i worktree `.git` pointer są dodatkowo read-only | cleanup po crash wymaga dalszego hardeningu |
| Code review jako bariera przed dryfem | **STRONGER** | deterministic risk scan poprzedza semantic review; correctness + architecture + security są wykonywane równolegle; jeden reject albo reviewer failure blokuje integration; candidate z materiałem sekretu nie jest wysyłany reviewerom | future specialty lanes mogą zostać dodane później |
| GitHub PR + CI | **ALIGNED** | deterministic integrator, branch push, PR, bounded CI wait, opcjonalny merge po PASS | required-check/branch-protection discovery i długie checkpointowane oczekiwanie jeszcze niepełne |
| MCP jako szyna narzędziowa | **PARTIAL** | neutralna konfiguracja MCP w `converge.yaml`, generowana do stable OpenCode | Converge nie wymusza konkretnego katalogu git/github/pytest/desktop MCP; część funkcji realizuje bezpieczniej deterministycznym kodem lokalnym |
| OpenWebUI jako punkt wejścia operatora | **ALIGNED** | natywny Workspace Tool nad Bearer-authenticated FastAPI; read-only status/compliance/evidence oraz confirmation-gated register/bootstrap/start/pause/resume/decision; durable state pozostaje w LangGraph | docelowy dashboard może poprawić ergonomię, ale nie jest wymagany do kontroli workflow |
| Łatwa rekonfiguracja projektu | **ALIGNED** | jeden `converge.yaml`, ścieżki względne do YAML, profiles/models/MCP/sandbox/quality/workflow oraz Valves dla operator bridge | pełny GUI editor projektu pozostaje opcjonalny |
| Minimalny HITL | **STRONGER** | przerwanie tylko dla deterministic risk policy lub wyczerpania bounded recovery; człowiek nie może zatwierdzić failing deterministic gate, hard-block secret material ani ominąć brakującego RED evidence; risk approval jest związane z hashem candidate diffu | szersze cross-language compatibility adapters mogą jeszcze zmniejszyć liczbę eskalacji |
| Least privilege / sandbox | **STRONGER** | deny-by-default role permissions + niezależny container boundary: RO Scout/Planner/Reviewers, RW tylko active Builder worktree, RO Git metadata/pointer, read-only root, cap-drop, no-new-privileges, resource limits, allowlisted ENV, rozdzielone sieci, internal-agent-network validation, timeout cleanup i host-only GitHub integration | production image hardening i konkretne sieci/toolchainy pozostają deployment-specific |
| Dual-memory / context rotation | **ALIGNED** | każda inwokacja OpenCode jest świeżą sesją bez `--continue`/`--session`; continuity pochodzi z LangGraph/evidence; deterministyczny bounded working memory jest advisory-only; context budget failuje przed model call, jeśli pełny authoritative core się nie mieści | token usage jest obecnie konserwatywnie estymowany; provider-reported cost/token telemetry pozostaje późniejszym hardeningiem |
| Evidence + compliance | **STRONGER** | SQLite checkpoints, evidence bundles, events, compliance snapshot, requirement verifiers, baseline/candidate regression policy, TDD baseline/RED/GREEN evidence, deterministic `risk.json`, candidate risk fingerprint oraz per-invocation context ledger | docelowo metrics/tracing i storage dla multi-worker |

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

## Context budget i fresh-session continuity

Każde wywołanie OpenCode jest niezależną, świeżą sesją. Converge nie przekazuje `--continue` ani
`--session`; nie istnieje więc ukryta historia modelu, od której zależy następna iteracja. Długotrwała
ciągłość pochodzi z checkpointowanego LangGraph state oraz jawnych artefaktów.

Prompt jest dzielony na dwie klasy:

```text
AUTHORITATIVE CORE
  - immutable requirement statements + source anchors
  - Task Envelope / acceptance criteria
  - pełny review diff
  - bieżące deterministic quality/review evidence wymagane przez daną rolę

ADVISORY CONTEXT
  - Repo Scout snapshot
  - bounded working-memory artifact z LangGraph state
```

Authoritative core nigdy nie jest automatycznie obcinany. Jeśli konserwatywny budżet wejścia wyliczony
z `model_profile.context_tokens`, `workflow.context_input_fraction` i output reserve jest za mały,
agent nie zostaje uruchomiony i pojawia się jawny `CONTEXT_BUDGET_EXCEEDED`. Kompaktować lub odrzucić
można wyłącznie sekcje oznaczone jako advisory.

Working memory zawiera bounded metadata continuity: compliance counts/requirement IDs, ostatni task,
krótkie review findings, retry counters, CI status i Scout base SHA. Nie kopiuje ani nie streszcza
immutable requirement statements, więc nie może zastąpić Source of Truth. Każdy agent call zapisuje
context evidence do `.converge/context-usage.jsonl`; Planner/Scout mają dodatkowo run-scoped evidence.

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

## Execution sandbox

`ExecutionSandbox` jest wspólną granicą dla OpenCode, quality gates i requirement verifiers. W
hardened `container` mode utrzymuje deterministic control plane poza kontenerem, a niezaufane wykonanie
przenosi do ograniczonego runtime:

```text
host: requirements / LangGraph / Git worktrees / commit / push / PR / merge
                         |
                    controlled mounts
                         v
container: Scout / Planner / Reviewers / Builder / tests / verifiers
```

Read-only role dostają wyłącznie RO repo. Builder jest jedynym writerem aktywnego worktree; shared
`.git` oraz sam `worktree/.git` pointer pozostają RO. Kontener ma read-only root, tmpfs scratch,
`cap-drop=ALL`, `no-new-privileges`, PID/RAM/CPU limits i selektywne ENV. Obrazy nie są pobierane
implicit, a timeout powoduje jawne `docker rm -f` nazwanej instancji.

Agent network może być wymagany jako rzeczywista Docker `Internal=true` network. Host-visible model
gateway i agent-visible gateway mają osobne endpointy, dzięki czemu host może używać loopbacku, ale
sandbox nie dostaje błędnej/pozornej ścieżki przez `127.0.0.1`. `opencode.attach_url` jest w container
mode zakazany, bo external server obchodziłby granicę wykonawczą.

Repo-controlled quality commands i verifiers działają przed finalnym scope gate; dopiero po nich
Converge mierzy zmienione pliki i diff budget. Pełny model operacyjny opisuje
[`EXECUTION_SANDBOX.md`](EXECUTION_SANDBOX.md).

## Deterministic TDD evidence

Dla tasków sklasyfikowanych jako `behavior` Task Envelope wymaga `tdd.mode=required`. Planner nie może
wstrzyknąć arbitralnego shell commandu: wybierany jest wyłącznie istniejący configured/discovered test
gate. Orkiestrator uruchamia ten gate na świeżym worktree jako baseline, zanim Builder dostanie fazę
RED.

RED jest akceptowany tylko wtedy, gdy zmiana jest test-only z dwóch niezależnych perspektyw. Musi
mieścić się w deklarowanych `tdd.test_paths`, ale sama deklaracja modelu nie jest authority: każdy
faktycznie zmieniony plik musi dodatkowo przejść deterministic cross-language test-artifact classifier
(np. `tests/`, `__tests__/`, `*_test.*`, `*.test.*`, `*.spec.*`, popularne `*Test` forms). Dzięki temu
Planner nie może nazwać `src/**` testami i otworzyć produkcyjnego write-setu w RED.

Wybrany test gate musi zakończyć się zwykłym failure, nie timeoutem ani brakiem narzędzia. Krótki,
single-line failure marker jest porównywany literalnie, nie jako regex, i musi pojawić się dopiero po
dodaniu nowego testu. Istniejące unrelated baseline failures są dopuszczalne, jeśli nowy sygnał jest
rzeczywiście novel.

Po zaakceptowanym RED Converge zapisuje SHA-256 wszystkich zmienionych artefaktów testowych. GREEN
wymaga, żeby każdy z nich nadal istniał z identycznym hashem oraz żeby ten sam deterministic test gate
przeszedł. Builder/repair nie mogą więc uzyskać GREEN przez osłabienie, skip, usunięcie albo przepisaną
wersję testu. Po wyczerpaniu bounded repair/replan specjalny TDD HITL oferuje wyłącznie replan lub stop;
nie istnieje human override prowadzący do integration bez RED.

## Deterministic repository-risk policy

Przed semantic review aktywny LangGraph uruchamia deterministyczną klasyfikację finalnego candidate
diffu. Risk scan nie korzysta z oceny LLM i nie ufa Plannerowi jako authority dla hard-block evidence.
Planner może deklarować ryzyka advisory/HITL, ale zastrzeżone blocking flags są odrzucane z części
deklaratywnej i mogą pojawić się w stanie grafu tylko jako rezultat klasyfikatora.

Klasyfikator obecnie obejmuje:

- high-confidence secret material i literalne sekrety — **BLOCK**, bez human override;
- nowe zależności od nazwanych sekretów środowiskowych — **HITL**;
- destrukcyjne operacje w ścieżkach migracji i usunięcie istniejącej migracji — **HITL**;
- usunięcie lub zmianę sygnatury publicznego Python API — **HITL**;
- jawne osłabienie albo utratę security primitives w auth/authz — **HITL**;
- małe zmiany na powierzchni auth bez utraty primitive — jawne evidence `observe`, bez automatycznej
  eskalacji.

Wykrycie materiału sekretu następuje przed zewnętrznym semantic review. W takim przypadku reviewerzy
nie otrzymują raw diffu, a evidence zapisuje tylko zredagowany finding. Dla approvable risków human
approval jest związane z SHA-256 dokładnego candidate diffu. Każdy repair zmieniający choć jeden bajt
unieważnia zgodę i wymusza świeżą klasyfikację oraz review.

To zachowuje zasadę LangGraph: modele produkują propozycje i oceny semantyczne, natomiast przejścia do
integration wynikają z jawnego stanu, deterministycznych węzłów i policy code.

## Priorytety dalszej zbieżności

Kolejność prac powinna maksymalizować autonomię bez zwiększania blast radius:

1. **Crash/chaos hardening** — leases, stale worktree cleanup, killed-process recovery i długie,
   checkpointowane oczekiwanie na CI.
2. **Compatibility adapters** — rozszerzenie public API/migration safety poza Python oraz bezpieczne
   shim/roll-forward strategies redukujące HITL.
3. **Remote policy/observability hardening** — branch protection/required checks, model fallback,
   metrics/tracing i multi-worker state.
4. **Deterministic architecture analyzers** — AST/import rules niezależne od custom project scripts.

## Kryterium docelowe

Projekt jest uznany za operacyjnie zbieżny z referencyjną wizją, gdy:

- immutable requirements nie mogą zostać zmienione ani wyparte przez derived summary;
- każde zadanie jest bounded i ma jednego writera;
- behavior-changing task ma wymagany, zweryfikowany RED przed GREEN;
- deterministyczne testy/policy oraz wszystkie wymagane review lanes przechodzą przed integracją;
- GitHub CI potwierdza candidate commit;
- system potrafi sam naprawiać/replanować do limitu i dopiero wtedy eskalować;
- operator może uruchamiać i kontrolować workflow z OpenWebUI bez przechowywania durable state w chacie;
- execution sandbox ogranicza szkody niezależnie od zachowania modelu;
- context rotation pozwala na długie projekty bez kumulowania nieograniczonej historii promptów.
