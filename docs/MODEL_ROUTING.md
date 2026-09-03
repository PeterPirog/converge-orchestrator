# Routing modeli agentów

Converge nie zakłada, że jeden model jest najlepszy do wszystkich etapów autonomicznego developmentu.
Domyślna konfiguracja jest **quality-first**: role o innych celach dostają inne modele, a Builder i
Reviewer są celowo rozdzielone między różne rodziny modeli.

To jest część architektury bezpieczeństwa jakościowego. Review wykonywane przez ten sam model, który
wytworzył implementację, ma większe ryzyko powtórzenia tych samych błędnych założeń. Model diversity
nie zastępuje deterministic quality gates, ale zwiększa wartość semantic review.

## Domyślny routing

| Rola | Domyślny model | Context | Dlaczego |
| --- | --- | ---: | --- |
| Planner | `deepseek-v4-pro:cloud` | 1,048,576 | frontier reasoning + tools/thinking; analiza architektury i wybór najmniejszego kolejnego kroku |
| Builder | `kimi-k2.7-code:cloud` | 262,144 | coding-focused long-horizon agent do wieloetapowego software engineering |
| Reviewer | `glm-5.3-flash:cloud` | 1,048,576 | inna rodzina niż Builder, duży kontekst i mocne coding/agentic reasoning |

Dokładne limity kontekstu są zapisane w `examples/converge.yaml` jako `context_tokens`. Converge
przekłada je na stable OpenCode `provider.models.<id>.limit.context`, dzięki czemu OpenCode może
zarządzać compaction względem rzeczywistego limitu custom gateway.

Domyślne profile pozostawiają `request_body: {}`. Jest to świadome: modele reasoning/coding mają
provider-specific ustawienia i ich optymalnych parametrów nie należy zgadywać w uniwersalnym
orkiestratorze. Converge uzyskuje powtarzalność przez Task Envelope, deterministic gates, compliance,
review i CI, a nie przez wymuszanie jednego `temperature` dla każdego modelu.

## Kandydaci do kolejnych ról

Kolejne etapy roadmapy rozszerzą review fan-out. Dla katalogu modeli referencyjnej instalacji
rekomendowany routing jest następujący:

| Przyszła rola | Model | Context | Zastosowanie |
| --- | --- | ---: | --- |
| Repo Scout / Triage | `deepseek-v4-flash:cloud` | 1,048,576 | szybkie mapowanie repo, logów i dużego kontekstu bez używania Plannera Pro |
| Security Reviewer | `gpt-oss:120b` | 131,072 | niezależna rodzina reasoning/tool-use, lokalne uruchomienie i drugi punkt widzenia |
| Local long-horizon fallback | `laguna-s-2.1:latest` | 262,144 | agentic coding i długie zadania bez zależności od cloud; bardzo duże wymagania pamięciowe |
| Coding fallback | `qwen3-coder-next:cloud` | 262,144 | coding/tool use jako zapasowy model implementacyjny |

Te profile nie są jeszcze automatycznie aktywowane przez bieżący trzy-agentowy graf. Dokument opisuje
zamierzony routing dla `ReviewCoordinator`, Repo Scout i provider failover z roadmapy; nie należy
dopisywać agentów do YAML, dopóki konkretna rola nie jest obsługiwana przez kod orchestratora.

## Dlaczego nie jeden model wszędzie

Domyślnie nie konfigurujemy np. `kimi-k2.7-code:cloud` jednocześnie jako Planner, Builder i Reviewer.
Model codingowy jest właściwym wyborem dla Writer loop, ale architektoniczny Planner powinien bardziej
optymalizować kolejność i zakres zmian, a Reviewer ma przede wszystkim szukać błędów i regresji.
Dodatkowo niezależność rodzin zmniejsza ryzyko skorelowanego self-review.

Podobnie model o największym context window nie jest automatycznie najlepszym Builderem. Converge
przekazuje Builderowi minimalny kontekst zadania i dokładne target requirement statements; duży context
jest szczególnie wartościowy dla Plannera i Reviewera analizujących szeroki repo/architecture context.

## Lokalność vs jakość

Referencyjny preset jest quality-first i używa modeli cloud tam, gdzie ich specjalizacja jest
najbardziej użyteczna. Jeżeli polityka projektu wymaga local-only, zachowaj role, ale zmień profile,
np.:

```yaml
models:
  profiles:
    planner:
      model: laguna-s-2.1:latest
      context_tokens: 262144
    builder:
      model: laguna-s-2.1:latest
      context_tokens: 262144
    reviewer:
      model: gpt-oss:120b
      context_tokens: 131072
```

W local-only nadal warto zachować inny model dla Reviewera. Jeżeli sprzęt nie mieści `laguna-s-2.1`,
wybierz mniejszy model z `tools` i najlepiej `thinking`, a następnie zwiększ znaczenie deterministic
verifiers i nie włączaj automerge przed walidacją E2E na danym stacku.

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
3. **Reviewer:** silne reasoning/coding oraz inna rodzina niż Builder.
4. **Security Reviewer:** niezależność modelu i dobra analiza kodu/tool output.
5. **Scout:** szybkość i koszt, ale nadal poprawne tool calling.

Nie wybieraj modelu wyłącznie na podstawie liczby parametrów albo długości context window.

## Referencje modeli użytych przez preset

- Kimi K2.7 Code: https://ollama.com/library/kimi-k2.7-code
- DeepSeek V4 Pro: https://ollama.com/library/deepseek-v4-pro
- GLM 5.3 Flash: https://ollama.com/library/glm-5.3-flash
- Laguna S 2.1: https://ollama.com/library/laguna-s-2.1
- gpt-oss: https://openai.com/open-models/
