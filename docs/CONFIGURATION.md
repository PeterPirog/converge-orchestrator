# Referencja konfiguracji `converge.yaml`

`converge.yaml` jest jedynym plikiem, który powinien być ręcznie edytowany dla konkretnego projektu.
Converge celowo oddziela konfigurację użytkownika od wygenerowanego runtime configu OpenCode.

Kanoniczny, w pełni komentowany wzorzec: [`examples/converge.yaml`](../examples/converge.yaml).

## Zasady nadrzędne

1. `converge.yaml` trzymaj poza repozytorium rozwijanym autonomicznie.
2. Nie zapisuj w nim sekretów; używaj nazw zmiennych środowiskowych.
3. `architecture.md` trzymaj poza target repo i ustaw read-only.
4. Zmiana projektu ma wymagać zmiany YAML, nie grafu LangGraph.
5. Jawne deterministic quality gates/verifiers są ważniejsze niż heurystyki LLM.
6. Generated OpenCode config jest artefaktem runtime, nie Source of Truth.
7. Project YAML może wybierać modele i custom/MCP tools, ale nie może osłabiać twardych granic ról.
8. Builder jest jedynym writerem; parallelism jest domyślnie ograniczony do read-only review.

## Top-level

```yaml
version: 1
project: {}
github: {}
opencode: {}
models: {}
agents: {}
quality: {}
workflow: {}
```

`version` musi obecnie wynosić `1`. Wcześniejszy płaski format jest nadal akceptowany dla
kompatybilności wstecznej, ale nowe projekty powinny używać formatu sekcyjnego.

Ścieżki względne są rozwiązywane względem katalogu `converge.yaml`, nie względem bieżącego working
directory procesu/PyCharm.

---

## `project`

```yaml
project:
  name: payments
  repo_path: ./repository
  requirements_path: ./architecture.md
  state_dir: null
  worktree_dir: null
  require_spec_read_only: true
```

### `name`

Opcjonalna nazwa czytelna dla człowieka.

### `repo_path`

Lokalny klon repozytorium docelowego. Jest bazowym checkoutem; Builder nie zapisuje bezpośrednio w tym
katalogu, tylko w izolowanym worktree.

### `requirements_path`

Read-only Markdown z architekturą/wymaganiami docelowymi. To immutable Source of Truth i powinien
znajdować się poza `repo_path`.

### `state_dir`

`null` oznacza `<repo-parent>/.converge`. Tutaj trafiają checkpointy LangGraph, `contract.json`,
compliance, evidence i generated OpenCode config.

### `worktree_dir`

`null` oznacza `<state_dir>/worktrees`.

### `require_spec_read_only`

Domyślnie `true`. W autonomous mode nie zaleca się wyłączania.

---

## `github`

```yaml
github:
  repo: acme/payments
  cli: gh
  base_branch: main
  branch_prefix: converge/
  auto_merge: false
  merge_method: squash
  ci_poll_seconds: 15
  ci_timeout_seconds: 1800
```

- `repo`: `owner/name`; `null` wyłącza GitHub PR/CI.
- `cli`: binarka GitHub CLI, domyślnie `gh`.
- `base_branch`: domyślnie `main`.
- `branch_prefix`: domyślnie `converge/`.
- `auto_merge`: merge dopiero po local gates, review, policy i remote CI.
- `merge_method`: `merge`, `squash` albo `rebase`.
- `ci_poll_seconds`: częstotliwość obserwacji CI.
- `ci_timeout_seconds`: twardy timeout CI observation.

Builder nie dostaje `gh` ani `git push`; finalna integracja jest deterministyczną warstwą
orkiestratora.

---

## `opencode`

Converge celuje w aktualny stabilny `opencode`.

```yaml
opencode:
  binary: opencode
  attach_url: null
  auto_approve: true
  generated_config_path: null
  mcp:
    servers: {}
```

### `binary`

Nazwa lub pełna ścieżka do stable OpenCode CLI.

### `attach_url`

`null` oznacza osobne lokalne `opencode run`. Persistent server może być wskazany np. jako
`http://127.0.0.1:4096`; musi widzieć ten sam filesystem repo/worktrees.

### `auto_approve`

Domyślnie `true`. Automatyzuje operacje, które OpenCode normalnie sklasyfikowałby jako `ask`, ale jawne
`deny` wygenerowane przez Converge nadal obowiązują. Nie jest to zamiennik OS/container sandboxu.

### `generated_config_path`

`null` oznacza `<state_dir>/opencode.generated.json`. Plik jest generowany i nadpisywany. Nie edytuj go
ręcznie.

Dla lokalnych agent calls Converge przekazuje tę samą policy jako high-precedence runtime config, żeby
config znajdujący się w target repo nie mógł osłabić safety boundaries.

### `mcp`

Neutralny user-facing format:

```yaml
opencode:
  mcp:
    servers:
      docs:
        type: remote
        url: https://mcp.example.com/mcp
        enabled: true
        oauth: false
        headers:
          X-API-Key: "{env:DOCS_MCP_API_KEY}"
```

Local MCP:

```yaml
opencode:
  mcp:
    servers:
      local-tools:
        type: local
        command: [python, -m, my_mcp_server]
        enabled: true
        environment:
          API_KEY: "{env:LOCAL_MCP_API_KEY}"
```

Converge konwertuje `servers` do stable OpenCode MCP config. Sekretów nie interpoluje.

MCP jest rozszerzeniem narzędzi, a nie źródłem finalnej authority: commit/push/PR/merge, requirement
hash i deterministic gates pozostają po stronie orkiestratora.

---

## `models`

### OpenWebUI gateway

```yaml
models:
  gateway:
    kind: openwebui
    provider_id: openwebui
    name: OpenWebUI
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
```

Wartość API key nie trafia do YAML ani generated JSON — zapisuje się tylko nazwa ENV.

### Generic OpenAI-compatible gateway

```yaml
models:
  gateway:
    kind: openai_compatible
    provider_id: internal-llm
    name: Internal LLM Gateway
    base_url: https://llm.example.com/v1
    api_key_env: INTERNAL_LLM_API_KEY
```

### Existing OpenCode provider

```yaml
models:
  gateway:
    kind: existing
  profiles:
    planner:
      model: anthropic/example-model
```

Dla `kind: existing` podaj pełne `provider/model` albo jawny `provider` w profilu.

### Domyślny quality-first routing

```yaml
models:
  profiles:
    planner:
      model: deepseek-v4-pro:cloud
      context_tokens: 1048576
      request_body: {}

    builder:
      model: kimi-k2.7-code:cloud
      context_tokens: 262144
      request_body: {}

    reviewer:
      model: glm-5.3-flash:cloud
      context_tokens: 1048576
      request_body: {}

    security:
      model: gpt-oss:120b
      context_tokens: 131072
      request_body: {}
```

Routing jest heterogeniczny celowo. Szczegóły: [`MODEL_ROUTING.md`](MODEL_ROUTING.md).

### Pola profilu

- `model`: dokładne ID zwracane przez gateway.
- `provider`: opcjonalny provider override.
- `name`: czytelna nazwa w generated OpenCode catalog.
- `variant`: opcjonalny wariant provider-specific.
- `context_tokens`: jawny limit context; trafia do OpenCode `limit.context`.
- `output_tokens`: opcjonalny jawny limit output; `null` = nie zgaduj.
- `request_body`: provider/model-specific request options; domyślnie `{}`.

Nie wymuszaj jednego `temperature` dla wszystkich modeli. Powtarzalność integracji zapewniają gates,
review i CI, nie sampling parameter.

Lista modeli widocznych przez gateway:

```bash
converge models --config /path/to/converge.yaml
```

---

## `agents`

Referencyjny preset:

```yaml
agents:
  planner:
    agent: converge-planner
    model_profile: planner
    timeout_seconds: 1800
    steps: 18
    tool_permissions: {}

  builder:
    agent: converge-builder
    model_profile: builder
    timeout_seconds: 3600
    steps: 60
    tool_permissions: {}

  correctness_reviewer:
    agent: converge-correctness-reviewer
    model_profile: reviewer
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}

  architecture_reviewer:
    agent: converge-architecture-reviewer
    model_profile: planner
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}

  security_reviewer:
    agent: converge-security-reviewer
    model_profile: security
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}
```

Starsze konfiguracje mogą nadal definiować pojedynczy `reviewer` i pominąć `workflow.review_roles`.

### Pola agenta

- `agent`: unikalny stable OpenCode runtime agent ID.
- `model_profile`: nazwa z `models.profiles`.
- `model`: alternatywa dla profilu; nie ustawiaj jednocześnie `model` i `model_profile`.
- `timeout_seconds`: twardy timeout pojedynczego agent call.
- `steps`: maksymalny budżet tool/model loop.
- `request_body`: per-agent override profilu; nie może nadpisać safety fields.
- `tool_permissions`: wyłącznie exact custom/MCP tool overrides.

OpenCode agent IDs muszą być unikalne. To zapobiega sytuacji, w której dwa logiczne lane'y nadpisują
sobie generated runtime definition.

### Twarde granice ról

Planner i wszystkie review roles:

- read-only;
- bez `edit`;
- ograniczony Git shell (`status`, `diff`, `log`);
- bez external directory;
- bez nested task delegation.

Builder:

- `edit` + lokalny shell w worktree;
- zakaz `git push`;
- zakaz `gh`;
- zakaz `git reset --hard` i `git clean`;
- bez external directory;
- bez nested task delegation.

Project YAML nie może nadpisać chronionych permissions takich jak `edit`, `bash`, `task`,
`external_directory`, `read` czy `websearch`.

---

## `quality`

```yaml
quality:
  auto_discover: true
  gates: []
  requirement_verifiers: {}
```

### `auto_discover`

Konserwatywnie rozpoznaje Python, Node, Go i Rust z manifestów/metadanych.

### `gates`

Jawne deterministic quality gates:

```yaml
quality:
  gates:
    - name: project-test-suite
      command: [make, test]
      required: true
      timeout_seconds: 1800
```

Exit code jest źródłem prawdy. Brak narzędzia i timeout są failure, nie opinią modelu.

### `requirement_verifiers`

Machine-verifiable evidence przypisane do immutable requirement ID:

```yaml
quality:
  requirement_verifiers:
    ARCH-017:
      - name: architecture-boundary
        command: [pytest, -q, tests/architecture/test_boundary.py]
        required: true
        timeout_seconds: 600
```

Reguły monotonic convergence:

- istniejący baseline `FAIL` może pozostać `FAIL` w unrelated change;
- mandatory `PASS` nie może przejść do non-PASS;
- target z configured verifier musi wykazać rzeczywisty postęp do `PASS` przed integracją.

Requirement IDs bierz z `<state_dir>/contract.json`.

---

## `workflow`

```yaml
workflow:
  max_repair_attempts: 3
  max_replans: 2
  max_iterations: 50
  max_diff_lines_hard: 1000
  review_roles:
    - correctness_reviewer
    - architecture_reviewer
    - security_reviewer
  max_parallel_reviews: 3
```

### Budżety autonomii

- `max_repair_attempts`: bounded repair loop przed replan/HITL.
- `max_replans`: bounded fresh planning po nieudanych repair loops.
- `max_iterations`: maksymalna liczba zmian w jednym autonomous run.
- `max_diff_lines_hard`: twarda górna granica Task Envelope diff.

Nie ustawiaj wartości „nieskończonych”.

### `review_roles`

Jawna lista wymaganych review lanes. Reguły:

- rola musi być skonfigurowana w `agents`;
- dozwolone są `reviewer`, `correctness_reviewer`, `architecture_reviewer`, `security_reviewer`;
- lista nie może zawierać duplikatów;
- każdy lane jest read-only;
- jeden `reject`, malformed JSON, timeout lub awaria procesu daje aggregate `reject`.

Brak `review_roles` zachowuje compatibility mode: workflow wywołuje pojedynczy `reviewer` tak jak
wcześniej.

### `max_parallel_reviews`

Zakres 1–16, domyślnie 3. Ogranicza concurrency, ale nie zmienia semantyki: wszystkie skonfigurowane
lane'y nadal muszą zakończyć się `pass`.

Jeżeli lokalny sprzęt nie utrzymuje kilku ciężkich modeli jednocześnie, ustaw `1` lub `2` zamiast
redukować liczbę wymaganych reviewerów.

---

## Generated `opencode.generated.json`

Converge tworzy provider/model catalog, kompletne agent definitions, permissions i MCP. Przykładowo
Builder ma jawne `edit: allow`, ale `git push`, `gh`, destructive reset/clean, external directory i task
delegation pozostają denied. Reviewerzy mają deny-by-default i tylko read/git-inspection capabilities.

Sekretów nie serializuje się do generated JSON.

---

## Walidacja konfiguracji

### Lista modeli

```bash
converge models --config /path/to/converge.yaml
```

### Pełny preflight

```bash
converge doctor --config /path/to/converge.yaml
```

`doctor` sprawdza ścieżki, read-only Source of Truth, narzędzia, requirement IDs, stack/gates, model
routing, ENV i live model catalog.

### Offline

```bash
converge doctor --offline --config /path/to/converge.yaml
```

Pomija live model catalog check; nie służy do omijania błędnej konfiguracji.

## Co zmieniać przy nowym projekcie

Najczęściej tylko:

```text
project.name
project.repo_path
project.requirements_path
github.repo
models.gateway.*      # jeżeli gateway jest inny
models.profiles.*     # jeżeli katalog modeli jest inny
opencode.mcp          # opcjonalne narzędzia projektu
quality.gates         # canonical project commands
quality.requirement_verifiers
workflow budgets      # tylko gdy projekt wymaga innych limitów
```

Nie zmieniaj grafu LangGraph ani core role permissions tylko dlatego, że przechodzisz na inne repo.
