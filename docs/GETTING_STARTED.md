# Pierwsze uruchomienie: PyCharm + OpenCode + OpenWebUI

Ten przewodnik prowadzi od świeżego klona Converge do pierwszego autonomicznego runu na innym
repozytorium. Dla jednego projektu docelowego powinieneś ręcznie utrzymywać **jeden plik**:
`converge.yaml`.

Converge nie traktuje historii chatu ani wygenerowanego configu OpenCode jako Source of Truth.
Architektura docelowa znajduje się w osobnym, read-only Markdown, a orkiestrator przypina jego SHA-256.

## 1. Zalecany układ katalogów

```text
/workspace/
├── converge-orchestrator/          # ten projekt
└── payments-target/
    ├── architecture.md             # READ ONLY, poza repozytorium
    ├── repository/                 # lokalny klon repo, które ma być rozwijane
    │   ├── .git/
    │   └── ...
    ├── converge.yaml               # jedyny plik konfiguracji tego projektu
    └── .converge/                  # stan generowany przez orkiestrator
        ├── contract.json
        ├── compliance.json
        ├── langgraph.sqlite
        ├── opencode.generated.json
        ├── evidence/
        └── worktrees/
```

`architecture.md` nie powinien znajdować się wewnątrz `repository/`. Builder pracuje wyłącznie w
worktree i nie powinien mieć możliwości przypadkowego dodania Source of Truth do commita.

## 2. Otwórz Converge w PyCharm

1. Sklonuj `PeterPirog/converge-orchestrator`.
2. Otwórz katalog repo jako projekt PyCharm.
3. Wybierz Python 3.11, 3.12 lub 3.13.
4. Utwórz `.venv`.
5. W terminalu PyCharm uruchom:

```bash
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

Sprawdź:

```bash
converge --help
converge-api --help
```

## 3. Sprawdź narzędzia wykonawcze

Converge celuje w **aktualny stabilny `opencode`**, nie w osobne beta `opencode2`.

```bash
git --version
opencode --version
gh --version
```

`gh` jest wymagane tylko, gdy `github.repo` jest ustawione. LangGraph jest trwałym state machine,
a OpenCode wykonuje repo-centric pracę agentów.

## 4. Przygotuj repozytorium docelowe

```bash
git clone git@github.com:YOUR_ORG/YOUR_REPO.git /workspace/payments-target/repository
cd /workspace/payments-target/repository
git status
```

Bazowy checkout powinien być czysty. Builder nie pracuje bezpośrednio na `main`; Converge tworzy
osobny branch i Git worktree dla każdego Task Envelope.

## 5. Przygotuj immutable architecture.md

Umieść wymagania np. tutaj:

```text
/workspace/payments-target/architecture.md
```

Linux/macOS:

```bash
chmod 444 /workspace/payments-target/architecture.md
```

Na Windows zalecany jest ACL NTFS odbierający prawo zapisu. Sam atrybut `ReadOnly` jest słabszą
barierą. Przy `project.require_spec_read_only: true` `converge doctor` odmówi startu, gdy plik jest
zapisywalny.

## 6. Skonfiguruj OpenWebUI

OpenWebUI jest domyślnym modelem gateway. Ustaw jego API key wyłącznie w środowisku procesu.

Linux/macOS:

```bash
export OPENWEBUI_API_KEY='sk-...'
```

PowerShell:

```powershell
$env:OPENWEBUI_API_KEY = 'sk-...'
```

Nigdy nie wpisuj wartości klucza do `converge.yaml`.

Domyślny gateway:

```yaml
models:
  gateway:
    kind: openwebui
    provider_id: openwebui
    name: OpenWebUI
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
```

Jeżeli OpenWebUI działa gdzie indziej, zmień tylko `base_url`.

## 7. Zobacz modele widoczne przez gateway

Po skopiowaniu YAML możesz użyć Converge zamiast ręcznego `curl`:

```bash
converge models --config /workspace/payments-target/converge.yaml
```

Polecenie wypisuje dokładne `id` z OpenWebUI `/api/models`. `doctor` później sprawdzi, czy modele
aktywnych agentów rzeczywiście istnieją.

## 8. Skopiuj jeden converge.yaml

```bash
cp /workspace/converge-orchestrator/examples/converge.yaml \
   /workspace/payments-target/converge.yaml
```

Minimalnie zmień:

```yaml
project:
  name: payments
  repo_path: /workspace/payments-target/repository
  requirements_path: /workspace/payments-target/architecture.md

github:
  repo: YOUR_ORG/YOUR_REPO
```

Ścieżki względne są rozwiązywane względem katalogu, w którym leży `converge.yaml`, dzięki czemu
working directory ustawione przez PyCharm nie zmienia ich znaczenia.

## 9. Domyślne modele agentów

Referencyjna instalacja ma już quality-first routing:

```yaml
models:
  profiles:
    planner:
      model: deepseek-v4-pro:cloud
    builder:
      model: kimi-k2.7-code:cloud
    reviewer:
      model: glm-5.3-flash:cloud
```

Dobór jest celowy:

- **Planner — `deepseek-v4-pro:cloud`**: szeroki reasoning i 1M context do analizy architektury;
- **Builder — `kimi-k2.7-code:cloud`**: coding-focused long-horizon agent do implementacji;
- **Reviewer — `glm-5.3-flash:cloud`**: inna rodzina niż Builder, mocne coding/agentic review.

Nie ustawiaj Buildera i Reviewera na ten sam model bez potrzeby. Niezależna rodzina modelu zmniejsza
ryzyko skorelowanych błędów. Deterministic gates, compliance i CI nadal są ważniejsze niż werdykt LLM.

Dodatkowe rekomendacje i local-only routing są w [MODEL_ROUTING.md](MODEL_ROUTING.md).

Jeśli któregoś ID nie ma w Twoim OpenWebUI, uruchom `converge models` i zmień wyłącznie odpowiedni
`models.profiles.<role>.model`.

## 10. Właściwości agentów

Profile modeli mówią **jaki model** ma wykonywać rolę. Sekcja `agents` mówi **jak długo i z jakim
budżetem wykonawczym** może pracować:

```yaml
agents:
  planner:
    agent: converge-planner
    model_profile: planner
    timeout_seconds: 1800
    steps: 18

  builder:
    agent: converge-builder
    model_profile: builder
    timeout_seconds: 3600
    steps: 60

  reviewer:
    agent: converge-reviewer
    model_profile: reviewer
    timeout_seconds: 1800
    steps: 24
```

To są limity, nie cele. Autonomia pozostaje bounded.

Domyślnie `request_body: {}` pozostawia parametry sampling/reasoning ustawieniom providera. Nie
wymuszamy uniwersalnego niskiego `temperature`, bo modele reasoning i coding mają różne optymalne
ustawienia. Provider-specific tuning rób dopiero po benchmarku na własnym repo.

## 11. Permissions OpenCode

Converge generuje kompletne runtime role o wyższym priorytecie niż config znaleziony w target repo.
Dzięki temu `repository/opencode.json` lub `repository/.opencode/` nie może osłabić granic roli.

Planner i Reviewer są read-only. Builder jest jedynym writerem w worktree, ale ma jawny zakaz m.in.:

```text
git push
git reset --hard
git clean
gh
external_directory
task/subagent delegation
```

Integracja Git/GitHub jest wykonywana przez deterministyczny kod orkiestratora dopiero po gate'ach.

## 12. MCP w tym samym pliku

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

Converge przekształca neutralne `servers` do formatu stabilnego OpenCode. Używaj `{env:NAME}` zamiast
sekretów. Włączaj tylko potrzebne serwery MCP; zbyt szeroki katalog tools zwiększa kontekst i ryzyko
błędnego wyboru narzędzia.

Per-agent `tool_permissions` mogą otwierać custom/MCP tools, ale nie mogą zmienić chronionych granic
`edit`, `bash`, `external_directory`, `task`, `read` itd.

## 13. Uwierzytelnij GitHub

```bash
gh auth login
gh auth status
```

Zalecane:

- brak direct push do `main`;
- required Actions checks;
- branch protection/ruleset;
- minimalne uprawnienia `gh` potrzebne Integratorowi.

Builder nie dostaje `gh` ani `git push`.

## 14. Uruchom converge doctor

```bash
converge doctor --config /workspace/payments-target/converge.yaml
```

`doctor` sprawdza m.in.:

- repo i architecture path;
- read-only Source of Truth i SHA-256;
- `opencode` i opcjonalnie `gh` w PATH;
- requirement IDs;
- stack i effective deterministic quality gates;
- requirement verifier IDs;
- rozwiązany model każdego aktywnego agenta;
- obecność ENV z kluczem gateway;
- live `/api/models` i dostępność wybranych modeli;
- generated OpenCode config.

Offline, wyłącznie do pominięcia live catalog check:

```bash
converge doctor --offline --config /workspace/payments-target/converge.yaml
```

Nie traktuj `--offline` jako obejścia błędnego ID modelu.

## 15. Co jest generowane

Po `doctor` powstaje:

```text
<state_dir>/opencode.generated.json
```

Plik zawiera stable OpenCode provider, model catalog, pełne agent role/permissions i MCP. Sekret nie
jest serializowany; zapisywana jest tylko referencja `{env:OPENWEBUI_API_KEY}`.

**Nie edytuj tego pliku.** Zmieniaj `converge.yaml`.

## 16. OpenCode lokalnie vs persistent server

Najprostszy pierwszy setup:

```yaml
opencode:
  attach_url: null
```

Wtedy każde lokalne `opencode run` dostaje wygenerowany config oraz high-precedence inline runtime
config od Converge.

Jeżeli chcesz używać serwera:

```bash
OPENCODE_CONFIG=/workspace/payments-target/.converge/opencode.generated.json \
  opencode serve --port 4096
```

Następnie:

```yaml
opencode:
  attach_url: http://127.0.0.1:4096
```

Serwer musi widzieć te same ścieżki repo/worktree.

## 17. Pierwszy autonomiczny run

```bash
converge run \
  --config /workspace/payments-target/converge.yaml \
  --thread-id payments-main
```

Albo uruchom control-plane API:

```bash
converge-api
```

Workflow zachowuje immutable intent, one-writer-per-worktree, deterministic gates, independent review,
bounded repair/replan, GitHub CI i exception-based HITL.

## 18. Zmiana na zupełnie inny projekt

Nie zmieniaj grafu. Utwórz np.:

```text
/workspace/orders-target/converge.yaml
```

Zmień tylko dane projektu: repo path, architecture path, GitHub repo, ewentualnie model profiles, MCP,
quality gates/verifiers i limity workflow. To jest podstawowy mechanizm wieloprojektowości Converge.

## 19. Najczęstsze problemy

### `configured models are not visible in the gateway`

Uruchom `converge models`, skopiuj dokładne ID i sprawdź uprawnienia API key w OpenWebUI.

### `missing environment variable OPENWEBUI_API_KEY`

Ustaw sekret w środowisku terminala/PyCharm, z którego startuje Converge.

### `OpenCode executable not found on PATH`

Sprawdź `opencode --version` w dokładnie tym samym środowisku procesu.

### `architecture requirements must be read-only`

Nadaj realne uprawnienia read-only. Nie wyłączaj tej kontroli w autonomous production run.

### Brak wykrytego build/test command

Dodaj jawny `quality.gates`. Auto-discovery jest celowo konserwatywne.

### OpenCode blokuje potrzebne polecenie Buildera

Najpierw sprawdź, czy polecenie nie narusza granic roli. Nie obchodź `git push`, `gh`, external path czy
destructive Git deny przez `tool_permissions`.

## Oficjalne referencje integracji

- Stable OpenCode configuration: https://opencode.ai/docs/config/
- Stable OpenCode providers: https://opencode.ai/docs/providers/
- Stable OpenCode agents: https://opencode.ai/docs/agents/
- Stable OpenCode MCP: https://opencode.ai/docs/mcp-servers/
- OpenWebUI API endpoints: https://docs.openwebui.com/reference/api-endpoints/
- OpenWebUI server-side tool calling: https://docs.openwebui.com/reference/server-side-tool-calling/
