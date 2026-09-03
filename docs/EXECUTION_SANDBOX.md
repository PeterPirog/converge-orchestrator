# Execution sandbox

Converge ma dwie warstwy bezpieczeństwa wykonania. Permissions OpenCode ograniczają to, o co agent może
poprosić na poziomie harnessu, natomiast `ExecutionSandbox` ogranicza proces na poziomie systemu
operacyjnego/kontenera. W autonomicznym środowisku, w którym kod z repozytorium i polecenia
model-controlled są traktowane jako niezaufane, docelowym profilem jest `sandbox.mode: container`.

`host` pozostaje domyślny wyłącznie dla kompatybilności istniejących konfiguracji i lokalnego
bootstrapu.

## Granica zaufania

```text
HOST / deterministic control plane
  Converge + LangGraph checkpoints
  immutable requirements hash/contract
  git worktree creation/cleanup
  commit + push
  GitHub PR / CI / merge
  operator API
        |
        | controlled bind mounts + allowlisted ENV
        v
CONTAINER / untrusted execution plane
  Scout / Planner / Reviewers (repository read-only)
  Builder (active worktree read-write)
  deterministic quality commands
  requirement verifiers
```

Model nie dostaje kontenera z Docker socketem ani hostowego `gh`. Integracja z GitHub pozostaje w
kontrolerze. Sandbox nie jest mechanizmem do uruchamiania całego Converge w kontenerze; jest granicą
wokół procesów, które wykonują kod projektu albo narzędzia sterowane przez agentów.

## Konfiguracja hardened

Minimalny profil:

```yaml
sandbox:
  mode: container
  engine: docker
  image: ghcr.io/acme/payments-converge-runtime@sha256:...
  agent_network: converge-ai
  quality_network: none
  agent_gateway_base_url: http://open-webui:8080/api
  require_internal_agent_network: true
  read_only_root: true
  pids_limit: 512
  memory: 8g
  cpus: 4.0
  tmpfs_size: 2g
  pass_env: []
  user: host
```

Pełny komentowany wzorzec znajduje się w [`examples/converge.yaml`](../examples/converge.yaml).

## Obraz runtime

Converge **nie wykonuje implicit pull**. `converge doctor` wymaga, aby skonfigurowany obraz był już
lokalnie dostępny. Zarządzanie buildem, pinowaniem digestu, podpisem i dystrybucją obrazu należy do
pipeline'u deploymentowego projektu.

Obraz musi zawierać co najmniej:

- stabilne `opencode` dostępne pod nazwą z `opencode.binary`;
- `/bin/sh` dla quality gates z `shell: true`;
- toolchain target repo, którego wymagają testy/build/lint/typecheck/verifiers;
- lokalne MCP executables, jeśli projekt używa MCP `type: local`;
- certyfikaty/CA potrzebne do połączenia z model gateway lub remote MCP.

Nie istnieje sensowny jeden uniwersalny obraz dla wszystkich projektów. Python, Node, Go, Rust,
kompilatory natywne, przeglądarki i bazy testowe mają inne wymagania. Dlatego `sandbox.image` jest
konfiguracją projektu, nie stałym obrazem Converge.

Przy obrazie z własnym `ENTRYPOINT` Converge jawnie ustawia pusty entrypoint i uruchamia dokładnie
kontrolowaną komendę. Zapobiega to przypadkowemu złożeniu np. `opencode opencode run`.

## Sieć agentów

Jeżeli `require_internal_agent_network: true`, `agent_network` musi być nazwaną siecią Docker z
`Internal=true`. `none` i `host` są odrzucane fail-closed.

Przykład:

```bash
docker network create --internal converge-ai
```

`converge doctor` sprawdza nie tylko istnienie sieci, ale także wartość jej flagi `Internal`.
Converge nie tworzy sieci automatycznie.

Wewnętrzna sieć ogranicza bezpośredni egress agenta, ale usługi, które agent ma wykorzystywać, muszą
być do niej jawnie podłączone. Typowo dotyczy to OpenWebUI/model gateway oraz kontrolowanego proxy dla
remote MCP. Nie należy podłączać do tej sieci Docker socketa ani usług administracyjnych hosta.

## Hostowy i kontenerowy endpoint OpenWebUI

`models.gateway.base_url` jest endpointem widzianym przez **hostowy** Converge, m.in. przez
`converge models` i live validation w `converge doctor`:

```yaml
models:
  gateway:
    kind: openwebui
    base_url: http://127.0.0.1:3000/api
```

Proces wewnątrz kontenera nie może użyć hostowego loopbacku. Dlatego hardened profile podaje osobny
adres widoczny z `agent_network`:

```yaml
sandbox:
  agent_gateway_base_url: http://open-webui:8080/api
```

W container mode generated OpenCode config używa `agent_gateway_base_url`, natomiast hostowe operacje
Converge nadal używają `models.gateway.base_url`. Loopback (`localhost`, `127.0.0.0/8`, `::1`) jako
runtime gateway dla sandboxowanych agentów jest odrzucany.

## `opencode.attach_url`

Container sandbox celowo nie wspiera `opencode.attach_url`. Attached OpenCode server wykonywałby
narzędzia w swoim własnym środowisku, poza procesem uruchomionym przez `ExecutionSandbox`, więc
pozornie włączony sandbox nie stanowiłby granicy wykonawczej.

W `sandbox.mode: container` ustaw:

```yaml
opencode:
  attach_url: null
```

Każda inwokacja pozostaje świeżą sesją OpenCode, a continuity jest utrwalane w LangGraph/evidence.

## Filesystem policy

Dla Scouta, Plannera i Reviewerów bieżący katalog repo/worktree jest bind-mountowany read-only.

Builder dostaje read-write wyłącznie aktywny worktree. Jednocześnie:

- wspólne metadata bazowego repo `.git` są bind-mountowane read-only;
- plik `worktree/.git`, który wskazuje shared worktree metadata, jest osobnym read-only overlay mount;
- ustawiane jest `GIT_OPTIONAL_LOCKS=0`;
- root filesystem kontenera jest domyślnie read-only;
- writable scratch jest ograniczony do kontrolowanego `/tmp` tmpfs;
- state directory jest montowany tylko wtedy, gdy dana operacja jawnie go potrzebuje, i wtedy read-only.

Dzięki temu Builder może zmieniać pliki projektu, ale nie może przepisać refs/shared Git metadata ani
przekierować worktree przez modyfikację jego `.git` pointera przed hostowym integration step.

## Process policy

Container invocation wymusza m.in.:

```text
--rm
--init
--pull=never
--cap-drop=ALL
--security-opt no-new-privileges:true
--read-only
--pids-limit <configured>
--memory <configured>
--cpus <configured>
--tmpfs /tmp:rw,nosuid,nodev,...
```

Na POSIX `user: host` uruchamia proces z UID/GID operatora. `user: image` pozostawia usera z obrazu i
powinien być używany tylko wtedy, gdy permissions bind mountów są świadomie przygotowane dla tego
użytkownika.

Każdy kontener otrzymuje unikalną nazwę `converge-...`. Jeżeli klient `docker run` przekroczy timeout,
Converge wykonuje `docker rm -f` dla tej konkretnej nazwy przed propagacją timeoutu. To ogranicza
ryzyko osieroconego procesu po zabiciu klienta Docker.

## Environment policy

Converge nie przekazuje hostowego środowiska do kontenera przez wildcard. Do środka trafiają wyłącznie:

- nazwy jawnie podane w `sandbox.pass_env`;
- `models.gateway.api_key_env`;
- zmienne wykryte z `{env:NAME}` w gateway headers i konfiguracji MCP;
- runtime vars wymagane przez Converge, np. `GIT_OPTIONAL_LOCKS`, `HOME`, `XDG_CACHE_HOME`;
- jawny `env` przekazany przez kontrolowany adapter danej operacji.

Do Docker CLI przekazywane są nazwy (`--env NAME`), nie wartości serializowane do command line lub
config JSON. Sekrety nadal muszą istnieć w środowisku uruchamiającym Converge.

## Quality network i finalny scope check

Quality gates i requirement verifiers korzystają z tego samego `ExecutionSandbox`, ale z osobnym
`quality_network`. Referencyjny hardened profil używa `none`. Jeżeli integration tests wymagają sieci,
powinna to być dedykowana, minimalna sieć projektu, a nie automatyczne `host`.

Repo-controlled quality commands i requirement verifiers mogą generować albo zmieniać pliki. Dlatego
aktywny LangGraph wykonuje je **przed** finalnym `changed_files` / diff-budget scope gate. Scope jest
mierzony dopiero po ostatniej kontrolowanej komendzie repo, więc plik nie może pojawić się już po
zaakceptowaniu zakresu zmiany.

## Preflight

Przed autonomous run wykonaj:

```bash
converge doctor --config /absolute/path/to/converge.yaml
```

W container mode `doctor` failuje, jeżeli:

- engine nie istnieje na PATH;
- image nie jest dostępny lokalnie;
- wymagany agent network nie istnieje lub nie jest Docker-internal;
- skonfigurowano `opencode.attach_url`;
- runtime model gateway wskazuje loopback kontenera;
- pozostałe zwykłe walidacje projektu/modeli/requirements nie przechodzą.

Preflight nie robi implicit pull, nie tworzy sieci i nie naprawia hosta. To celowa właściwość
fail-closed: provisioning ma być jawny i audytowalny.

## Co pozostaje poza granicą sandboxu

Sandbox nie zastępuje:

- immutable requirement hash/OS read-only;
- deterministic policy i compliance;
- independent review;
- GitHub branch protection/CI;
- project-specific dependency/security scanning;
- ochrony samego hostowego demona Docker;
- izolacji między tenantami na poziomie osobnych hostów/VM, jeśli wymagany jest silniejszy model
  zagrożeń niż lokalny Docker.

Najważniejsza własność jest kompozycyjna: nawet jeśli prompt/tool permissions zawiodą albo model
spróbuje wykonać niepożądaną komendę, jej blast radius nadal ogranicza niezależna granica
filesystem/network/process, a commit/push/merge pozostają poza agent containerem.
