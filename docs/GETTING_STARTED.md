# Pierwsze uruchomienie: PyCharm + OpenCode + OpenWebUI

Ten przewodnik prowadzi od świeżego klona Converge do pierwszego autonomicznego runu na innym
repozytorium. Docelowo powinieneś edytować **jeden plik projektu**: `converge.yaml`.

Converge nie przechowuje wymagań architektonicznych w promptach ani w Skills. Plik architektury jest
zewnętrznym, read-only Source of Truth, a orkiestrator sprawdza jego SHA-256 podczas workflow.

## 1. Zalecany układ katalogów

Najprostszy układ dla jednego projektu docelowego:

```text
/workspace/
├── converge-orchestrator/          # ten projekt
├── payments-target/
│   ├── architecture.md             # READ ONLY, poza repozytorium
│   ├── repository/                 # lokalny klon repo, które ma być rozwijane
│   │   ├── .git/
│   │   └── ...
│   ├── converge.yaml               # jedyny plik konfiguracji tego projektu
│   └── .converge/                  # stan generowany przez orkiestrator
│       ├── contract.json
│       ├── compliance.json
│       ├── langgraph.sqlite
│       ├── opencode.generated.json
│       ├── evidence/
│       └── worktrees/
```

`architecture.md` **nie powinien znajdować się wewnątrz `repository/`**. Dzięki temu Builder nie może
przypadkowo dodać go do commita, a izolacja worktree jest prostsza.

## 2. Otwórz Converge w PyCharm

1. Sklonuj repozytorium Converge.
2. Otwórz katalog `converge-orchestrator` jako projekt w PyCharm.
3. Ustaw interpreter Python 3.11, 3.12 lub 3.13.
4. Utwórz virtualenv, np. `.venv`.
5. W terminalu PyCharm uruchom:

```bash
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

Po instalacji powinny być dostępne polecenia:

```bash
converge --help
converge-api --help
```

## 3. Zainstaluj i sprawdź narzędzia wykonawcze

W systemowym `PATH` powinny być dostępne:

```bash
git --version
opencode --version
gh --version
```

Jeśli nie używasz GitHub dla danego projektu, `gh` nie jest wymagane i w `converge.yaml` możesz
ustawić `github.repo: null`.

Converge używa OpenCode jako repo-centric execution harness. LangGraph steruje kolejnością etapów,
ale nie zastępuje OpenCode w edycji kodu.

## 4. Przygotuj repozytorium docelowe

Sklonuj repo, które Converge ma rozwijać:

```bash
git clone git@github.com:YOUR_ORG/YOUR_REPO.git /workspace/payments-target/repository
```

Przed startem bazowy checkout powinien być czysty:

```bash
cd /workspace/payments-target/repository
git status
```

Nie uruchamiaj Buildera bezpośrednio na `main`. Converge tworzy osobny Git worktree i osobny branch dla
każdego Task Envelope.

## 5. Przygotuj immutable architecture.md

Umieść wymagania poza repozytorium, np.:

```text
/workspace/payments-target/architecture.md
```

Na Linux/macOS ustaw plik jako read-only:

```bash
chmod 444 /workspace/payments-target/architecture.md
```

Na Windows zalecany jest ACL NTFS odbierający bieżącemu kontu prawo zapisu. Sam atrybut `ReadOnly`
jest słabszym zabezpieczeniem niż ACL i nie powinien być jedyną barierą w środowisku produkcyjnym.

`converge doctor` odmówi startu, jeżeli `project.require_spec_read_only: true`, a plik ma ustawione bity
zapisu.

## 6. Skonfiguruj dostęp do modeli przez OpenWebUI

OpenWebUI wystawia endpoint Chat Completions pod `/api/chat/completions` i katalog modeli pod
`/api/models`. OpenCode może używać tego endpointu jako custom OpenAI-compatible provider.

W OpenWebUI utwórz API key dla konta, które ma dostęp do potrzebnych modeli. Następnie ustaw sekret
wyłącznie w środowisku procesu Converge/OpenCode.

Linux/macOS:

```bash
export OPENWEBUI_API_KEY='sk-...'
```

PowerShell:

```powershell
$env:OPENWEBUI_API_KEY = 'sk-...'
```

Nie wpisuj klucza do `converge.yaml` i nie commituj go do Git.

Sprawdź katalog modeli:

```bash
curl -s \
  -H "Authorization: Bearer $OPENWEBUI_API_KEY" \
  http://127.0.0.1:3000/api/models
```

Skopiuj dokładne `id` modeli, których chcesz użyć. Te wartości wpiszesz do `models.profiles`.

Jeżeli OpenWebUI działa na innym hoście lub porcie, zmień `models.gateway.base_url`. Dla OpenWebUI
wartość powinna kończyć się na `/api`, np.:

```yaml
base_url: http://192.168.1.20:3000/api
```

## 7. Utwórz jeden converge.yaml

Skopiuj wzorzec:

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

models:
  gateway:
    kind: openwebui
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
  profiles:
    planner:
      model: EXACT_MODEL_ID_FOR_PLANNING
    builder:
      model: EXACT_MODEL_ID_FOR_CODING
    reviewer:
      model: EXACT_MODEL_ID_FOR_REVIEW
```

Role agentów są już podpięte do profili:

```yaml
agents:
  planner:
    agent: converge-planner
    model_profile: planner
  builder:
    agent: converge-builder
    model_profile: builder
  reviewer:
    agent: converge-reviewer
    model_profile: reviewer
```

Możesz używać tego samego modelu dla wszystkich ról. Dla lepszej niezależności review zalecane jest
jednak użycie innego modelu lub przynajmniej osobnej sesji/review profile.

## 8. Właściwości modeli i agentów

Parametry requestu modelu umieszczaj w profilu modelu:

```yaml
models:
  profiles:
    planner:
      model: my-reasoning-model
      request_body:
        temperature: 0.1
    builder:
      model: my-coding-model
      request_body:
        temperature: 0.1
```

Parametry wykonawcze roli umieszczaj pod `agents`:

```yaml
agents:
  builder:
    agent: converge-builder
    model_profile: builder
    timeout_seconds: 2400
    steps: 40
```

Jeżeli provider obsługuje warianty OpenCode, możesz ustawić `variant`. Nie zakładaj, że `high`, `max`
lub inna nazwa istnieje dla każdego modelu; wariant jest provider-specific.

## 9. MCP w tym samym pliku

OpenCode V2 MCP można skonfigurować bez tworzenia osobnego `opencode.json`:

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

Converge kopiuje tę sekcję do generowanego `opencode.generated.json`. Używaj `{env:NAME}` zamiast
wpisywania sekretów bezpośrednio.

Włączaj tylko MCP potrzebne danej instalacji. Duży katalog narzędzi MCP zwiększa kontekst modelu i
może pogorszyć jakość działania.

## 10. Uwierzytelnij GitHub

Jeżeli używasz PR/CI:

```bash
gh auth login
gh auth status
```

Zalecane ustawienia repozytorium GitHub:

- brak bezpośrednich pushy do `main`;
- wymagane GitHub Actions checks przed merge;
- branch protection/ruleset;
- token/gh z minimalnym zakresem wymaganym do tworzenia PR i merge przez Integratora.

Builder nie dostaje uprawnień do `git push` ani `gh` w profilu OpenCode.

## 11. Uruchom converge doctor

Przed pierwszym runem:

```bash
converge doctor --config /workspace/payments-target/converge.yaml
```

`doctor` sprawdza między innymi:

- istnienie repozytorium i architecture.md;
- read-only architecture.md;
- obecność `opencode` i opcjonalnie `gh` w PATH;
- SHA-256 Source of Truth;
- skompilowane requirement IDs;
- wykryty stack i effective quality gates;
- poprawność requirement verifier IDs;
- rozwiązany model dla każdego agenta;
- zmienną środowiskową z kluczem gateway;
- live `/models` i obecność skonfigurowanych modeli;
- ścieżkę wygenerowanego OpenCode config.

Jeżeli pracujesz offline i chcesz pominąć tylko live model catalog check:

```bash
converge doctor --offline --config /workspace/payments-target/converge.yaml
```

Nie używaj `--offline` jako stałego obejścia błędnego model ID.

## 12. Co jest generowane automatycznie

Po `doctor` lub pierwszym wywołaniu agenta powstaje:

```text
<state_dir>/opencode.generated.json
```

Plik zawiera:

- OpenWebUI/OpenAI-compatible provider;
- wyłącznie nazwę zmiennej ENV z kluczem, nigdy wartość sekretu;
- model catalog potrzebny profilom;
- model/variant/steps/request overrides dla agentów;
- MCP z `converge.yaml`.

**Nie edytuj tego pliku ręcznie.** Zmień `converge.yaml` i wygeneruj go ponownie przez `doctor`.

## 13. OpenCode lokalnie vs opencode serve

Najprostszy i zalecany pierwszy setup:

```yaml
opencode:
  attach_url: null
```

Wtedy każde wywołanie `opencode run` dostaje `OPENCODE_CONFIG` automatycznie od Converge.

Jeżeli chcesz używać długotrwałego serwera OpenCode, najpierw uruchom `doctor`, a następnie wystartuj
serwer z tym samym wygenerowanym configiem.

Linux/macOS:

```bash
OPENCODE_CONFIG=/workspace/payments-target/.converge/opencode.generated.json \
  opencode serve --port 4096
```

PowerShell:

```powershell
$env:OPENCODE_CONFIG = 'C:\workspace\payments-target\.converge\opencode.generated.json'
opencode serve --port 4096
```

Następnie:

```yaml
opencode:
  attach_url: http://127.0.0.1:4096
```

Jeżeli serwer OpenCode działa na innej maszynie, ścieżki w `--dir` dotyczą filesystemu tej maszyny.
Nie ustawiaj remote `attach_url`, jeśli serwer nie widzi tych samych repo/worktree paths.

## 14. Pierwszy run

CLI:

```bash
converge run \
  --config /workspace/payments-target/converge.yaml \
  --thread-id payments-main
```

Lub uruchom control-plane API:

```bash
converge-api
```

Następnie zarejestruj projekt i uruchom run przez API opisane w README.

## 15. Jak przełączyć Converge na zupełnie inny projekt

Nie zmieniaj grafu ani kodu orkiestratora. Utwórz drugi plik, np.:

```text
/workspace/orders-target/converge.yaml
```

Zmień tylko:

- `project.repo_path`;
- `project.requirements_path`;
- `github.repo`;
- model profiles, jeśli ten projekt wymaga innych modeli;
- opcjonalne MCP;
- quality gates i requirement verifiers;
- limity workflow.

To jest podstawowy mechanizm wieloprojektowości Converge.

## 16. Najczęstsze problemy

### `configured models are not visible in the gateway`

Model ID w `models.profiles.*.model` nie jest identyczny z ID zwracanym przez OpenWebUI `/api/models`.
Skopiuj dokładne ID i uruchom `doctor` ponownie.

### `missing environment variable OPENWEBUI_API_KEY`

Ustaw sekret w środowisku procesu PyCharm/terminala. Nie dodawaj klucza do YAML.

### `OpenCode executable not found on PATH`

Sprawdź `opencode --version` w **tym samym terminalu/interpreter environment**, z którego uruchamiasz
Converge. PyCharm może mieć inny PATH niż systemowa powłoka.

### `architecture requirements must be read-only`

Ustaw realne uprawnienia read-only dla `architecture.md`. Nie wyłączaj tej kontroli w projekcie, który ma
działać autonomicznie.

### Build/test command nie został wykryty

Dodaj jawny `quality.gates` zamiast zmieniać kod grafu. Auto-discovery jest celowo konserwatywne.

### OpenCode pyta o zgodę mimo trybu autonomicznego

Sprawdź `opencode.auto_approve: true`. Jawne `deny` nadal obowiązują; jeśli blokowana jest potrzebna
operacja, najpierw oceń, czy nie narusza ona modelu bezpieczeństwa one-writer-per-worktree.

## Oficjalne referencje integracji

- OpenCode configuration: https://opencode.ai/v2/docs/config
- OpenCode providers: https://opencode.ai/v2/docs/providers
- OpenCode agents: https://opencode.ai/v2/docs/agents
- OpenCode MCP: https://opencode.ai/v2/docs/mcp-servers
- OpenWebUI API endpoints: https://docs.openwebui.com/reference/api-endpoints/
- OpenWebUI server-side tool calling: https://docs.openwebui.com/reference/server-side-tool-calling/
