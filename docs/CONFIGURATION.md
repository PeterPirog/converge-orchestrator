# Referencja konfiguracji `converge.yaml`

`converge.yaml` jest jedynym plikiem, który powinien być ręcznie edytowany dla konkretnego projektu.
Converge celowo oddziela konfigurację użytkownika od generowanej konfiguracji OpenCode.

Zalecany wzorzec znajduje się w `examples/converge.yaml`.

## Zasady ogólne

1. `converge.yaml` trzymaj poza repozytorium, które jest rozwijane autonomicznie.
2. Nie zapisuj w nim sekretów.
3. `architecture.md` trzymaj poza repozytorium i ustaw read-only.
4. Zmiana projektu powinna wymagać zmiany YAML, a nie kodu LangGraph.
5. Jawne quality gates i requirement verifiers są ważniejsze niż heurystyki auto-discovery.
6. Generated OpenCode config jest artefaktem runtime i nie jest Source of Truth.

## Top-level

Zalecany układ:

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

Converge nadal akceptuje wcześniejszy płaski format konfiguracji dla kompatybilności wstecznej, ale
nowe projekty powinny używać formatu sekcyjnego.

---

## `project`

### `project.name`

Opcjonalna nazwa czytelna dla człowieka.

```yaml
project:
  name: payments
```

Nie zmienia nazwy repo GitHub ani branchy.

### `project.repo_path`

Wymagana absolutna ścieżka do lokalnego klona repozytorium docelowego.

```yaml
project:
  repo_path: /workspace/payments/repository
```

To jest bazowy checkout. Builder nie pracuje bezpośrednio tutaj; Converge tworzy worktree.

### `project.requirements_path`

Wymagana ścieżka do immutable Markdown Source of Truth.

```yaml
project:
  requirements_path: /workspace/payments/architecture.md
```

Plik nie powinien znajdować się wewnątrz `repo_path`.

### `project.state_dir`

```yaml
project:
  state_dir: null
```

`null` oznacza:

```text
<repo-parent>/.converge
```

Tutaj trafiają checkpointy, compliance, generated OpenCode config i evidence.

### `project.worktree_dir`

```yaml
project:
  worktree_dir: null
```

`null` oznacza:

```text
<state_dir>/worktrees
```

### `project.require_spec_read_only`

Domyślnie `true`.

```yaml
project:
  require_spec_read_only: true
```

Wyłączenie tej kontroli obniża gwarancję Immutable Intent i nie jest zalecane dla autonomous mode.

---

## `github`

### Przykład

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

### `github.repo`

Repo w formacie `owner/name`.

```yaml
github:
  repo: acme/payments
```

Ustaw `null`, jeśli chcesz pracować bez GitHub PR/CI.

### `github.cli`

Domyślnie `gh`. Można podać pełną ścieżkę do binarki.

### `github.base_branch`

Domyślnie `main`.

### `github.branch_prefix`

Domyślnie `converge/`.

### `github.auto_merge`

Domyślnie `false`.

Przy `true` merge następuje dopiero po lokalnych gate'ach, independent review, policy oraz remote CI.

### `github.merge_method`

Dozwolone:

```text
merge
squash
rebase
```

### `github.ci_poll_seconds`

Częstotliwość sprawdzania GitHub CI.

### `github.ci_timeout_seconds`

Twardy timeout obserwacji CI.

---

## `opencode`

### Przykład

```yaml
opencode:
  binary: opencode
  attach_url: null
  auto_approve: true
  generated_config_path: null
  mcp:
    servers: {}
```

### `opencode.binary`

Nazwa lub ścieżka OpenCode CLI.

### `opencode.attach_url`

`null` oznacza lokalne `opencode run`.

```yaml
opencode:
  attach_url: null
```

Dla persistent server:

```yaml
opencode:
  attach_url: http://127.0.0.1:4096
```

W trybie attach provider/MCP/agent request configuration musi być również widoczna dla serwera
OpenCode. Najprościej uruchomić `opencode serve` z `OPENCODE_CONFIG` wskazującym wygenerowany plik.

### `opencode.auto_approve`

Domyślnie `true`.

Converge dodaje `--auto` do `opencode run`. OpenCode automatycznie zatwierdza operacje, które normalnie
miałyby efekt `ask`, ale jawne `deny` w profilu agenta pozostają zablokowane.

To nie zastępuje sandboxingu systemowego.

### `opencode.generated_config_path`

`null` oznacza:

```text
<state_dir>/opencode.generated.json
```

Plik jest nadpisywany przez Converge. Nie edytuj go ręcznie.

### `opencode.mcp`

Ta sekcja jest przekazywana do OpenCode V2 jako `mcp`.

Przykład local stdio MCP:

```yaml
opencode:
  mcp:
    servers:
      local-tools:
        type: local
        command: [python, -m, my_mcp_server]
        environment:
          API_KEY: "{env:MY_MCP_KEY}"
```

Przykład remote Streamable HTTP MCP:

```yaml
opencode:
  mcp:
    servers:
      docs:
        type: remote
        url: https://mcp.example.com/mcp
        oauth: false
        headers:
          X-API-Key: "{env:DOCS_MCP_API_KEY}"
```

Converge nie interpoluje sekretów. Składnia `{env:NAME}` jest obsługiwana przez OpenCode.

---

## `models`

Sekcja `models` rozwiązuje dwa problemy:

1. definiuje transport/provider do modeli;
2. definiuje wielokrotnie używalne profile modeli dla ról agentów.

### OpenWebUI jako gateway

```yaml
models:
  gateway:
    kind: openwebui
    provider_id: openwebui
    name: OpenWebUI
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
```

Dla OpenWebUI `base_url` powinien kończyć się na `/api`. Generated OpenCode provider dokłada ścieżkę
Chat Completions zgodnie z providerem OpenAI-compatible.

Converge przechowuje wyłącznie nazwę ENV, np. `OPENWEBUI_API_KEY`. Wartość sekretu nie trafia do
generowanego JSON.

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

Możesz także dodać statyczne niesekretne nagłówki:

```yaml
models:
  gateway:
    kind: openai_compatible
    provider_id: internal-llm
    base_url: https://llm.example.com/v1
    api_key_env: INTERNAL_LLM_API_KEY
    headers:
      X-Tenant: platform-team
```

### Existing OpenCode providers

Jeżeli provider jest już skonfigurowany globalnie w OpenCode:

```yaml
models:
  gateway:
    kind: existing
  profiles:
    planner:
      model: anthropic/claude-sonnet-example
    builder:
      model: openai/gpt-example
```

Jeśli `model` zawiera `/` i `provider` nie jest podany, Converge traktuje go jako kompletny
`provider/model`.

### Profile modeli

```yaml
models:
  profiles:
    planner:
      model: my-reasoning-model
      name: Planner reasoning model
      variant: null
      request_body:
        temperature: 0.1
      request_headers: {}
```

#### `model`

Model ID. Dla gateway OpenWebUI powinien być identyczny z ID zwracanym przez `/api/models`.

#### `provider`

Opcjonalny provider override.

```yaml
models:
  profiles:
    reviewer:
      provider: another-provider
      model: review-model
```

Jeżeli `provider` nie jest podany, profil używa `models.gateway.provider_id`, chyba że `model` jest już
pełnym `provider/model`.

#### `name`

Czytelna nazwa wpisywana do generated provider catalog.

#### `variant`

OpenCode model variant. Jest provider-specific.

```yaml
variant: high
```

Nie ustawiaj wariantu na podstawie przypuszczenia. Używaj tylko wariantów dostępnych dla konkretnego
modelu/providera.

#### `request_body`

Dowolne parametry requestu przekazywane przez OpenCode do providera.

```yaml
request_body:
  temperature: 0.1
```

Możliwe pola zależą od modelu i providera. Nie wszystkie modele akceptują `temperature`.

#### `request_headers`

Per-model/agent request headers. Nie umieszczaj tu sekretów, jeśli plik ma być przechowywany w Git.

---

## `agents`

Minimalna konfiguracja trzech ról:

```yaml
agents:
  planner:
    agent: converge-planner
    model_profile: planner
    timeout_seconds: 1200
    steps: 12

  builder:
    agent: converge-builder
    model_profile: builder
    timeout_seconds: 2400
    steps: 40

  reviewer:
    agent: converge-reviewer
    model_profile: reviewer
    timeout_seconds: 1200
    steps: 16
```

### `agent`

ID profilu OpenCode, obecnie dostarczanego przez:

```text
.opencode/agents/converge-planner.md
.opencode/agents/converge-builder.md
.opencode/agents/converge-reviewer.md
```

System prompt i twarde role bezpieczeństwa pozostają w repo Converge. Konfiguracja projektu wybiera
modele i parametry runtime.

### `model_profile`

Nazwa z `models.profiles`.

### `model`

Alternatywa dla `model_profile`; bezpośredni OpenCode `provider/model`.

```yaml
agents:
  planner:
    agent: converge-planner
    model: openai/gpt-example
```

Nie ustawiaj jednocześnie `model` i `model_profile`.

### `variant`

Per-agent override wariantu. Ma pierwszeństwo przed wariantem profilu.

### `timeout_seconds`

Twardy timeout całego `opencode run` dla roli.

### `steps`

Maksymalna liczba kroków model/tool loop konfigurowana dla profilu OpenCode.

### `request_body` / `request_headers`

Per-agent override. Są mergowane na profil modelu; wartości agenta mają pierwszeństwo.

Przykład:

```yaml
models:
  profiles:
    coding:
      model: code-model
      request_body:
        temperature: 0.1

agents:
  builder:
    agent: converge-builder
    model_profile: coding
    request_body:
      max_completion_tokens: 16000
```

---

## Jak powstaje `opencode.generated.json`

Dla konfiguracji:

```yaml
models:
  gateway:
    kind: openwebui
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
  profiles:
    builder:
      model: code-model

agents:
  builder:
    agent: converge-builder
    model_profile: builder
    steps: 40
```

Converge generuje logicznie odpowiednik:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "openwebui": {
      "name": "OpenWebUI",
      "env": ["OPENWEBUI_API_KEY"],
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:3000/api"
      },
      "models": {
        "code-model": {
          "name": "code-model"
        }
      }
    }
  },
  "agents": {
    "converge-builder": {
      "model": "openwebui/code-model",
      "steps": 40
    }
  }
}
```

Wartość `OPENWEBUI_API_KEY` nie jest serializowana.

---

## `quality`

### `quality.auto_discover`

Domyślnie `true`.

```yaml
quality:
  auto_discover: true
```

Aktualny inspector rozpoznaje konserwatywnie:

- Python;
- Node;
- Go;
- Rust.

Nie uruchamia komendy tylko dlatego, że „wydaje się standardowa”; szuka odpowiednich manifestów,
metadanych lub skryptów projektu.

### `quality.gates`

Jawne deterministic quality gates.

```yaml
quality:
  gates:
    - name: tests
      command: [make, test]
      required: true
      timeout_seconds: 1800
```

`command` może być listą argv albo stringiem. Shell jest domyślnie wyłączony.

```yaml
- name: special-check
  command: "./scripts/check.sh --strict"
  shell: false
```

Ustaw `shell: true` tylko gdy komenda rzeczywiście wymaga interpretacji przez shell.

### `quality.requirement_verifiers`

Mapowanie konkretnych requirement IDs na deterministic evidence.

```yaml
quality:
  requirement_verifiers:
    ARCH-017:
      - name: architecture-boundary
        command: [pytest, -q, tests/architecture/test_boundary.py]
        required: true
```

Reguły konwergencji:

- baseline `FAIL` może pozostać `FAIL` w unrelated PR;
- mandatory `PASS` nie może przejść do non-PASS;
- jeśli Task Envelope targetuje skonfigurowany verifier, przynajmniej jeden target musi poprawić się z
  non-PASS do PASS.

Requirement ID pobieraj z wygenerowanego `contract.json`. Nie zgaduj ID.

---

## `workflow`

```yaml
workflow:
  max_repair_attempts: 3
  max_replans: 2
  max_iterations: 50
  max_diff_lines_hard: 1000
```

### `max_repair_attempts`

Maksymalna liczba lokalnych repair loops przed replan/HITL.

### `max_replans`

Maksymalna liczba fresh replans przed wyjątkiem wymagającym operatora.

### `max_iterations`

Twardy budżet liczby iteracji autonomous convergence.

### `max_diff_lines_hard`

Globalny hard limit diff size. Task Envelope może ustawić niższy limit, ale nie może przekroczyć tej
wartości.

---

## Sekrety i środowisko

Nie zapisuj w YAML:

- OpenWebUI API key;
- GitHub token;
- MCP API keys;
- hasła OpenCode server;
- provider secrets.

Używaj ENV lub native credential stores.

Przykład `.env` jest wygodny lokalnie, ale nie powinien być commitowany:

```text
OPENWEBUI_API_KEY=...
DOCS_MCP_API_KEY=...
```

Jeżeli PyCharm uruchamia Converge z Run Configuration, dodaj te ENV do konfiguracji uruchomieniowej albo
upewnij się, że PyCharm dziedziczy je z procesu nadrzędnego.

---

## Różne projekty, ten sam orkiestrator

Przykład:

```text
/workspace/
├── converge-orchestrator/
├── payments/
│   ├── architecture.md
│   ├── repository/
│   └── converge.yaml
└── orders/
    ├── architecture.md
    ├── repository/
    └── converge.yaml
```

Każdy projekt ma osobny:

- Source of Truth;
- state_dir;
- worktrees;
- compliance matrix;
- model profiles;
- MCP/gates;
- GitHub target.

Kod Converge jest wspólny.

---

## Walidacja

Po każdej istotnej zmianie YAML uruchom:

```bash
converge doctor --config /path/to/converge.yaml
```

Dla pracy bez dostępu do gateway:

```bash
converge doctor --offline --config /path/to/converge.yaml
```

`--offline` pomija live model catalog, ale nie pomija walidacji ścieżek, read-only Source of Truth,
model profile references, stacku, quality policy ani verifier IDs.

---

## Kompatybilność z wcześniejszym płaskim YAML

Starszy format nadal działa:

```yaml
repo_path: /workspace/project/repository
requirements_path: /workspace/project/architecture.md
github_repo: acme/project
opencode_binary: opencode
agents:
  planner:
    agent: converge-planner
    model: openai/example-model
```

Nie jest jednak zalecany dla nowych instalacji, ponieważ nie skaluje się równie czytelnie do gateway,
model profiles, MCP i wieloprojektowej administracji.
