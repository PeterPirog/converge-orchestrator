# Referencja konfiguracji `converge.yaml`

`converge.yaml` jest jedynym plikiem, który powinien być ręcznie edytowany dla konkretnego projektu.
Converge celowo oddziela konfigurację użytkownika od wygenerowanego runtime configu OpenCode.

Wzorzec: [`examples/converge.yaml`](../examples/converge.yaml).

## Zasady nadrzędne

1. `converge.yaml` trzymaj poza repozytorium rozwijanym autonomicznie.
2. Nie zapisuj w nim sekretów.
3. `architecture.md` trzymaj poza target repo i ustaw read-only.
4. Zmiana projektu ma wymagać zmiany YAML, nie grafu LangGraph.
5. Jawne deterministic quality gates/verifiers są ważniejsze niż heurystyki.
6. Generated OpenCode config jest artefaktem runtime, nie Source of Truth.
7. Project YAML może wybierać modele i custom/MCP tools, ale nie może osłabiać twardych granic ról.

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

Ścieżki względne w `converge.yaml` są rozwiązywane względem katalogu tego YAML, nie względem bieżącego
working directory procesu/PyCharm.

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

Opcjonalna nazwa czytelna dla człowieka. Nie zmienia GitHub repo ani branchy.

### `repo_path`

Lokalny klon repozytorium docelowego. Jest bazowym checkoutem; Builder nie zapisuje bezpośrednio w tym
katalogu, tylko w izolowanym worktree.

### `requirements_path`

Read-only Markdown z architekturą/wymaganiami docelowymi. To Source of Truth. Plik powinien znajdować
się poza `repo_path`.

### `state_dir`

`null` oznacza:

```text
<repo-parent>/.converge
```

Tutaj trafiają checkpointy LangGraph, `contract.json`, compliance, evidence i runtime OpenCode config.

### `worktree_dir`

`null` oznacza `<state_dir>/worktrees`.

### `require_spec_read_only`

Domyślnie `true`. Wyłączanie tego w autonomous mode nie jest zalecane.

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

Builder nie dostaje `gh` ani `git push`; integracja jest deterministyczną warstwą orkiestratora.

---

## `opencode`

Converge celuje w aktualny **stabilny `opencode`**. Osobne beta `opencode2` nie jest domyślnym runtime.

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

`null` oznacza lokalne `opencode run` dla każdego agent call.

Dla persistent server:

```yaml
opencode:
  attach_url: http://127.0.0.1:4096
```

Serwer musi widzieć ten sam filesystem repo/worktrees.

### `auto_approve`

Domyślnie `true`. Converge może automatyzować promptowane operacje, ale jawne `deny` wygenerowane dla
roli nadal obowiązują. To nie zastępuje systemowego sandboxu.

### `generated_config_path`

`null` oznacza `<state_dir>/opencode.generated.json`. Plik jest generowany i nadpisywany. Nie edytuj go
ręcznie.

Dla lokalnych agent calls Converge dodatkowo przekazuje tę samą definicję jako high-precedence runtime
config, żeby config znajdujący się w target repo nie mógł osłabić safety policy.

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

Converge konwertuje `servers` do formatu stable OpenCode. Nie interpoluje sekretów. Używaj `{env:NAME}`.

---

## `models`

### OpenWebUI gateway

Domyślna integracja:

```yaml
models:
  gateway:
    kind: openwebui
    provider_id: openwebui
    name: OpenWebUI
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
```

`base_url` kończy się na `/api`; provider OpenAI-compatible używa endpointu Chat Completions poniżej tej
bazy. Wartość klucza nie trafia do YAML ani generated JSON — tylko referencja ENV.

### Generic OpenAI-compatible gateway

```yaml
models:
  gateway:
    kind: openai_compatible
    provider_id: internal-llm
    name: Internal LLM Gateway
    base_url: https://llm.example.com/v1
    api_key_env: INTERNAL_LLM_API_KEY
    headers:
      X-Tenant: platform-team
```

`headers` powinny zawierać tylko niesekretne wartości albo provider-supported ENV substitution.

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
      name: DeepSeek V4 Pro - architecture planner
      request_body: {}

    builder:
      model: kimi-k2.7-code:cloud
      name: Kimi K2.7 Code - implementation builder
      request_body: {}

    reviewer:
      model: glm-5.3-flash:cloud
      name: GLM 5.3 Flash - independent reviewer
      request_body: {}
```

Routing jest celowo heterogeniczny. Builder i Reviewer używają różnych rodzin modeli. Szczegółowe
uzasadnienie i alternatywy: [`MODEL_ROUTING.md`](MODEL_ROUTING.md).

### `model`

Dokładne ID modelu zwracane przez OpenWebUI `/api/models`. Sprawdź je komendą:

```bash
converge models --config /path/to/converge.yaml
```

### `provider`

Opcjonalny provider override dla profilu.

### `name`

Czytelna nazwa w generated OpenCode model catalog.

### `variant`

Opcjonalny wariant provider-specific. Nie zakładaj, że `high`, `max` itd. istnieje dla każdego modelu.

### `request_body`

Provider/model-specific opcje requestu. Domyślne profile mają `{}` celowo.

```yaml
request_body:
  temperature: 1.0
  top_p: 0.95
```

Nie ustawiaj parametrów na podstawie nazwy modelu lub ogólnej heurystyki. Benchmarkuj przez dokładnie ten
sam OpenWebUI/OpenCode transport. Determinism integracyjny jest zapewniany przez gates, nie przez
sampling parameter.

### `request_headers`

Opcjonalne per-profile nagłówki. Nie wpisuj tu sekretów w plaintext.

---

## `agents`

Domyślne bounded budgets:

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

  reviewer:
    agent: converge-reviewer
    model_profile: reviewer
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}
```

### `agent`

Stable OpenCode runtime agent ID. Converge generuje prompt i twarde permissions dla znanych ról.

### `model_profile`

Nazwa z `models.profiles`.

### `model`

Alternatywa dla `model_profile`, np. `openai/example-model`. Nie ustawiaj jednocześnie obu.

### `timeout_seconds`

Twardy timeout pojedynczego `opencode run`.

### `steps`

Maksymalny budżet tool/model loop. Jest limitem, nie celem.

### `request_body`

Per-agent override profilu. Nie może nadpisać pól bezpieczeństwa runtime agent definition.

### `tool_permissions`

Może służyć do włączenia konkretnych custom/MCP tools, np.:

```yaml
agents:
  planner:
    tool_permissions:
      docs_*: allow
```

Nie można z YAML nadpisać chronionych kluczy takich jak `edit`, `bash`, `external_directory`, `task`,
`read`, `websearch` itd. Próba osłabienia granicy roli powoduje błąd configu.

### Twarde granice ról

Planner/Reviewer:

- read-only;
- bez edit;
- ograniczony Git shell (`status`, `diff`, `log`);
- bez external directory;
- bez nested task delegation.

Builder:

- edit + lokalny shell w worktree;
- zakaz `git push`;
- zakaz `gh`;
- zakaz `git reset --hard` i `git clean`;
- bez external directory;
- bez nested task delegation.

---

## Jak powstaje `opencode.generated.json`

Dla OpenWebUI Converge generuje logicznie stable OpenCode config w rodzaju:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "openwebui": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenWebUI",
      "options": {
        "baseURL": "http://127.0.0.1:3000/api",
        "apiKey": "{env:OPENWEBUI_API_KEY}"
      },
      "models": {
        "kimi-k2.7-code:cloud": {
          "name": "Kimi K2.7 Code - implementation builder"
        }
      }
    }
  },
  "agent": {
    "converge-builder": {
      "model": "openwebui/kimi-k2.7-code:cloud",
      "steps": 60,
      "permission": {
        "edit": "allow",
        "external_directory": "deny"
      }
    }
  }
}
```

Rzeczywisty generated agent zawiera pełną policy, w tym shell deny rules. Sekret nie jest
serializowany.

---

## `quality`

```yaml
quality:
  auto_discover: true
  gates: []
  requirement_verifiers: {}
```

### `auto_discover`

Rozpoznaje konserwatywnie Python, Node, Go i Rust z manifestów/metadanych. Nie zgaduje dowolnych
komend projektu.

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

`command` może być argv list albo string. `shell` jest domyślnie `false`.

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

Reguły:

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
```

- `max_repair_attempts`: bounded repair loop przed replan/HITL.
- `max_replans`: bounded fresh planning po nieudanych repair loops.
- `max_iterations`: maksymalna liczba zmian w jednym autonomous run.
- `max_diff_lines_hard`: twarda górna granica Task Envelope diff; Planner może ustawić mniejszą.

Te limity są częścią safety modelu. Nie ustawiaj wartości „nieskończonych”.

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

Pomija wyłącznie live model catalog check. Nie służy do omijania błędnego ID modelu.

## Co zmieniać przy nowym projekcie

Najczęściej tylko:

```text
project.name
project.repo_path
project.requirements_path
github.repo
models.gateway.*     # jeśli gateway jest inny
models.profiles.*    # jeśli katalog modeli jest inny
opencode.mcp         # opcjonalnie
quality.*
workflow.*
```

Nie powinno być potrzeby edycji kodu LangGraph ani generated OpenCode JSON.
