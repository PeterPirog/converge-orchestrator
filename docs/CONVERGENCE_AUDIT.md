# Audyt zbieżności z referencyjną architekturą autonomicznych agentów

Ten dokument śledzi zgodność Converge z referencyjnymi założeniami autonomicznego workflow
software-engineering: niezmienny plik architektury, wyspecjalizowane role, małe iteracje, TDD,
niezależny review, Git/GitHub, MCP, minimalny HITL, trwały LangGraph oraz prostą rekonfigurację
między projektami.

Audyt rozróżnia **cel architektoniczny** od konkretnej technologii opisanej w materiale referencyjnym.
Stan trwały i przejścia procesu należą do LangGraph/control plane; OpenCode jest repo-centric executor,
OpenWebUI jest punktem operatorskim, a GitHub jest zewnętrzną barierą PR/CI/merge.

## Stan ogólny

Aktualnie **14 z 15 obszarów** jest zgodnych albo zaimplementowanych mocniej niż w referencji, a jeden
obszar (`MCP jako szyna narzędziowa`) pozostaje częściowy z wyboru architektonicznego. Nie oznacza to,
że projekt jest już production-complete: pozostały hardening crash/chaos, szersze cross-language
compatibility/architecture adapters i produkcyjny multi-worker storage.

Najważniejsze wcześniejsze luki operacyjne zostały istotnie zmniejszone. LangGraph ma trwałe run leases,
retry-safe side effects i checkpointowane oczekiwanie na GitHub CI. Worktree utworzony przed crashem
jest adoptowany zamiast kasowany; commit/push/PR/merge są odporne na ponowne wykonanie węzła. Długie CI
nie trzyma workera: graf przechodzi przez `ci_poll -> ci_wait(interrupt) -> ci_poll`, a serwis odtwarza
timer po restarcie z checkpointu.

Warstwa GitHub weryfikuje także lokalny `origin`, klasyczne branch protection oraz efektywne
`required_status_checks` z GitHub Rulesets. Gdy polityki chronionej gałęzi nie można odczytać lub jest
malformed, CI pozostaje `pending` — nigdy false-PASS. Ownership-aware cleanup jest już wdrożony;
największą pozostałą luką recovery jest pełny E2E chaos suite, a nie sam checkpointing.

## Macierz zbieżności

| Obszar | Status | Implementacja Converge | Pozostała luka |
| --- | --- | --- | --- |
| Niezmienny Markdown jako Source of Truth | **STRONGER** | plik poza target repo, OS read-only, SHA-256 pin, sprawdzanie przed krytycznymi przejściami, traceable `contract.json` | brak krytycznej luki |
| Przeciwdziałanie architectural drift | **STRONGER** | stable requirement IDs, source anchors, exact requirement injection, compliance, deterministic verifiers i monotonic regression policy | semantic-only requirements nadal wymagają niezależnego review |
| Deterministyczny kontroler nad LLM | **STRONGER** | LangGraph + Pydantic state + policy code; LLM nie może ominąć gate, sterować merge ani sam wytworzyć hard-block evidence | brak krytycznej luki |
| Planner / Worker / Reviewer | **STRONGER** | Scout RO, Planner RO, Builder jako jedyny writer, niezależne correctness/architecture/security review lanes RO | specialty analyzers mogą być rozszerzane |
| Autonomiczny TDD / repair loop | **ALIGNED** | behavior task wymaga baseline, test-only RED, novel literal marker, deterministic test-artifact check, SHA freeze i GREEN; bounded repair/replan; brak human bypass | `change_kind` może być dalej wzmacniany regułami językowymi |
| Izolacja Git | **STRONGER** | osobny deterministic worktree per task, safe adoption po crashu, RO shared Git metadata w sandboxie, ownership-aware terminal-resource GC | pełne E2E chaos tests |
| Code review jako bariera przed dryfem | **STRONGER** | deterministic risk scan przed semantic review; trzy niezależne lanes; reject albo reviewer failure blokuje integration; secret material nie trafia do reviewerów | dalsze specialty lanes opcjonalne |
| GitHub PR + CI | **STRONGER** | retry-safe push/PR/merge, checkpointable CI machine wait, origin validation, classic branch protection + effective Rulesets required checks, App-ID-aware matching, fail-closed protected policy | jawna flaky-job retry policy i dodatkowe E2E failure tests |
| MCP jako szyna narzędziowa | **PARTIAL** | neutralna konfiguracja MCP w `converge.yaml`, generowana do OpenCode; MCP dostępne dla agentów zgodnie z rolą | część krytycznych operacji Git/GitHub/test policy celowo pozostaje deterministycznym kodem zamiast delegacji do MCP |
| OpenWebUI jako punkt wejścia operatora | **ALIGNED** | Workspace Tool nad Bearer-authenticated FastAPI; confirmation-gated mutations; status/compliance/evidence/interrupts; durable state poza chatem | dashboard pozostaje ergonomicznym rozszerzeniem |
| Łatwa rekonfiguracja projektu | **ALIGNED** | jeden `converge.yaml`, ścieżki względne, model profiles, agents, MCP, sandbox, quality, verifiers i workflow | GUI editor opcjonalny |
| Minimalny HITL | **STRONGER** | HITL tylko dla risk policy/ambiguity lub wyczerpania bounded recovery; failing deterministic gates, hard secret BLOCK i brak RED nie są approvable | compatibility adapters mogą dalej zmniejszać eskalacje |
| Least privilege / sandbox | **STRONGER** | role permissions + container boundary: RO Scout/Planner/Reviewers, RW tylko Builder worktree, RO Git metadata, read-only root, cap-drop, no-new-privileges, resource limits, ENV/network policy i timeout cleanup | pinned production images i deployment-specific hardening |
| Dual-memory / context rotation | **ALIGNED** | świeża sesja OpenCode per agent attempt; continuity w LangGraph/evidence; authoritative core bez silent truncation; advisory-only compaction; bounded jawny model fallback z profile-specific budgets | provider-reported token/cost telemetry |
| Evidence + compliance | **STRONGER** | LangGraph/SQLite checkpoints, event/evidence bundles, persistent compliance, verifier evidence, TDD RED/GREEN, risk fingerprint, CI policy evidence i context ledger | produkcyjny shared storage, backup i metrics/tracing |

## LangGraph pozostaje źródłem prawdy o przebiegu

Aktywny przepływ jest jawnie grafowy. Modele wykonują ograniczone role, ale nie posiadają kontroli nad
przejściami procesu. Uproszczony przebieg wygląda następująco:

```text
bootstrap -> spec guard -> scout -> planner -> worktree
                                    |
                                    v
                         TDD baseline / RED / build
                                    |
                                    v
                       quality + scope + risk scan
                                    |
                                    v
                   parallel independent read-only review
                                    |
                                    v
                      integrate -> PR -> ci_poll
                                        |
                               pending  v
                                  ci_wait interrupt
                                        |
                                  auto resume
                                        v
                                     ci_poll
                                        |
                              PASS -> merge -> refresh
                                        |
                              next requirement / end
```

`ci_wait` jest machine interruptem, nie HITL. `wake_at` jest częścią checkpointu LangGraph. Worker i run
lease są zwalniane na czas oczekiwania, a `ScheduledRunController` odtwarza timer z checkpointu po
restarcie usługi. Dzięki temu wielogodzinne CI nie wymaga żywego procesu ani historii czatu.

## Crash recovery i semantyka at-least-once

LangGraph może po awarii powtórzyć węzeł z side effectem, dlatego krytyczne operacje są projektowane
jako `ensure`, a nie `create blindly`:

- `create_worktree()` adoptuje wyłącznie worktree o oczekiwanej deterministic path/branch i nie robi
  automatycznego force-cleanup niejasnego stanu;
- `integrate` rozpoznaje candidate commit utworzony przed utratą checkpointu i może ponowić push;
- `ensure_pull_request()` wykorzystuje istniejący otwarty PR zamiast tworzyć duplikat;
- `merge()` rozpoznaje already-merged PR;
- SQLite run lease ma owner/TTL/heartbeat i blokuje równoległe wykonanie tego samego `thread_id`, ale po
  śmierci procesu może zostać przejęty po expiry;
- checkpointowane CI wait nie utrzymuje lease przez okres bezczynności.

Pozostały hardening nie polega już na „dodaniu checkpointów”. Automatyczny GC ma trwałe rekordy
własności i chroni worktree wskazywane przez active/recoverable/waiting run; główną luką jest pełny
E2E chaos suite potwierdzający te własności przy kill/restart w rzeczywistym procesie.

## GitHub remote policy

Converge nie ufa samemu `github.repo` z konfiguracji. Przed rejestracją projektu GitHub-backed i przed
rzeczywistym transportem `gh` lokalny `origin` musi wskazywać ten sam kanoniczny `github.com/owner/repo`.
Mismatched albo non-GitHub remote failuje przed PR/CI/merge side effects.

Dla chronionej gałęzi CI gate buduje efektywną politykę z dwóch źródeł:

```text
classic branch protection required checks
                 +
active GitHub Rulesets required_status_checks
                 |
                 v
        effective required-check set
                 |
           candidate SHA checks
```

Rulesets są pobierane dla konkretnej base branch i obejmują aktywne reguły repo/organization zwrócone
przez GitHub. `integration_id` rulesetu jest mapowany na GitHub App ID check-runu. Check o tej samej
nazwie z innej aplikacji nie spełnia wymagania. Klasyczna i rulesetowa lista są sumowane, nie
nadpisywane. Unrelated checki pozostają evidence, ale nie mogą ani spełnić, ani oblać autorytatywnego
required-check gate.

Jeśli chroniona polityka nie jest autorytatywna, malformed albo niedostępna, wynik pozostaje `pending`.
Converge nie próbuje kopiować całej logiki GitHub reviews/merge queue/signatures — GitHub pozostaje
ostatecznym enforcement point przy merge.

## Context, review i sandbox

Każde wywołanie OpenCode jest świeżą sesją. Continuity pochodzi wyłącznie z checkpointowanego LangGraph
state, Repo Scout snapshotu i jawnego working-memory/evidence. Immutable requirement statements, Task
Envelope i pełny review diff należą do authoritative core i nie są cicho skracane. Przekroczenie
budżetu core failuje przed model call; kompaktowane mogą być jedynie sekcje advisory.

Review jest równoległe wyłącznie dla read-only lanes. Deterministyczny agregator wymaga wszystkich
skonfigurowanych lanes; execution failure reviewera nie staje się PASS.

Awaria wykonania modelu może uruchomić wyłącznie skończoną, jawnie skonfigurowaną sekwencję primary
retry i fallback profiles. Każda próba zachowuje role permissions i świeżą sesję, ponownie przechodzi
context budget oraz zapisuje model/profile/exit evidence bez raw output. Malformed output albo
semantic reject nie są maskowane jako provider failure.

`ExecutionSandbox` otacza OpenCode, quality gates i requirement verifiers. Deterministyczny Git/GitHub
integrator pozostaje na hoście. Builder jest jedynym writerem worktree; reszta ról ma mount RO. W
container mode działają read-only root, drop capabilities, no-new-privileges, limity zasobów, tmpfs,
ENV allowlist, sieci kontrolowane i cleanup kontenera po timeout.

## TDD i deterministic risk policy

Dla `change_kind=behavior` Planner musi dostarczyć structured TDD contract wskazujący istniejący test
gate i test paths. RED jest ważny tylko dla deterministycznie rozpoznawalnych artefaktów testowych i
normalnego test failure z nowym literalnym markerem. Po RED hashe testów są zamrażane; GREEN musi
przejść na tym samym gate bez zmiany zaakceptowanych testów.

Przed semantic review finalny diff przechodzi deterministic risk classification. Wykryty secret
material jest hard BLOCK i raw diff nie jest wtedy wysyłany reviewerom. Destructive migration,
public-API compatibility i krytyczne auth/authz zmiany mogą wymagać HITL, ale approval jest związane z
SHA-256 dokładnego candidate diffu i wygasa po repair.

## Priorytety dalszej zbieżności

Kolejność prac po domknięciu effective GitHub policy:

1. **Cross-language compatibility adapters** — public API/data migration safety i bezpieczne
   shim/roll-forward strategies redukujące HITL.
2. **Crash/chaos completion** — E2E kill/restart testy dla service/OpenCode/provider/integrate/CI-wait.
3. **Flake-aware CI policy** — selektywny retry tylko dla jawnie sklasyfikowanych flaky jobs.
4. **Production state + observability** — PostgreSQL checkpointer/control registry, backup,
   OpenTelemetry/metrics i opcjonalny LangSmith bez przenoszenia evidence poza system.
5. **Cross-language architecture analyzers** — deterministic dependency rules poza Python AST/import.

## Kryterium docelowe

Projekt jest uznany za operacyjnie zbieżny z referencyjną wizją, gdy:

- immutable requirements nie mogą zostać zmienione ani wyparte przez derived summary;
- każde zadanie jest bounded i ma jednego writera;
- behavior-changing task ma zweryfikowany RED przed GREEN;
- deterministic gates i wszystkie wymagane review lanes przechodzą przed integracją;
- GitHub required CI policy potwierdza dokładny candidate commit;
- workflow można bezpiecznie wznowić po crashu bez utraty lub duplikowania side effects;
- system sam naprawia/replanuje do limitu i dopiero potem eskaluje wyjątki;
- OpenWebUI steruje procesem bez przechowywania durable state w chacie;
- execution sandbox ogranicza blast radius niezależnie od zachowania modelu;
- context rotation pozwala na długie projekty bez kumulowania nieograniczonej historii;
- cleanup po awarii nigdy nie usuwa zasobu należącego do aktywnego/recoverable runu.
