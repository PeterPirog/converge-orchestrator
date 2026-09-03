# Routing modeli agentów

Converge nie zakłada, że jeden model jest najlepszy do wszystkich etapów autonomicznego developmentu.
Domyślna konfiguracja jest **quality-first**: role o innych celach dostają inne modele, a Builder i
Reviewer są celowo rozdzielone między różne rodziny modeli.

To jest część architektury bezpieczeństwa jakościowego. Review wykonywane przez ten sam model, który
wytworzył implementację, ma większe ryzyko powtórzenia tych samych błędnych założeń. Model diversity
nie zastępuje deterministic quality gates, ale zwiększa wartość semantic review.

## Domyślny routing

| Rola | Domyślny model | Dlaczego |
| --- | --- | --- |
| Planner | `deepseek-v4-pro:cloud` | frontier reasoning, tools/thinking, bardzo duży kontekst; nadaje się do analizy architektury i wyboru najmniejszego kolejnego kroku |
| Builder | `kimi-k2.7-code:cloud` | coding-focused agentic model przeznaczony do długich, wieloetapowych zadań software-engineering |
| Reviewer | `glm-5.3-flash:cloud` | inna rodzina niż Builder, duży kontekst, mocne coding/agentic reasoning; dobre niezależne review |

Domyślne profile pozostawiają `request_body: {}`. Jest to świadome: modele reasoning/coding mają
provider-specific ustawienia i ich optymalnych parametrów nie należy zgadywać w uniwersalnym
orkiestratorze. Converge uzyskuje powtarzalność przez Task Envelope, deterministic gates, compliance,
review i CI, a nie przez wymuszanie bardzo niskiego `temperature` dla każdego modelu.

## Kandydaci do kolejnych ról

Kolejne etapy roadmapy rozszerzą review fan-out. Dla katalogu modeli referencyjnej instalacji
rekomendowany routing jest następujący:

| Przyszła rola | Model | Zastosowanie |
| --- | --- | --- |
| Repo Scout / Triage | `deepseek-v4-flash:cloud` | szybkie mapowanie repo, logów i dużego kontekstu bez używania najdroższego Plannera |
| Security Reviewer | `gpt-oss:120b` | niezależna rodzina reasoning/tool-use, lokalne uruchomienie i dobry drugi punkt widzenia |
| Local long-horizon fallback | `laguna-s-2.1:latest` | agentic coding i długie zadania bez zależności od cloud; wymaga dużej pamięci |
| Coding fallback | `qwen3-coder-next:cloud` | coding/tool use jako zapasowy model implementacyjny |

Te profile nie są jeszcze automatycznie aktywowane przez bieżący trzy-agentowy graf. Dokument opisuje
zamierzony routing dla `ReviewCoordinator` i provider failover z roadmapy; nie należy dopisywać agentów
do YAML, dopóki konkretna rola nie jest obsługiwana przez kod orchestratora.

## Dlaczego nie jeden model wszędzie

Domyślnie nie konfigurujemy np. `kimi-k2.7-code:cloud` jednocześnie jako Planner, Builder i Reviewer.
Model codingowy jest właściwym wyborem dla Writer loop, ale architektoniczny Planner powinien bardziej
optymalizować kolejność i zakres zmian, a Reviewer ma przede wszystkim szukać błędów i regresji.
Dodatkowo niezależność rodzin zmniejsza ryzyko skorelowanego self-review.

Podobnie model o największym context window nie jest automatycznie najlepszym Builderem. Converge
przekazuje agentom minimalny kontekst zadania; duży context jest szczególnie wartościowy dla Plannera
i Reviewera analizujących szeroki repo/architecture context.

## Lokalność vs jakość

Referencyjny preset jest quality-first i używa modeli cloud tam, gdzie ich specjalizacja jest
najbardziej użyteczna. Jeżeli polityka projektu wymaga local-only, zachowaj role, ale zmień profile,
np.:

```yaml
models:
  profiles:
    planner:
      model: laguna-s-2.1:latest
    builder:
      model: laguna-s-2.1:latest
    reviewer:
      model: gpt-oss:120b
```

W local-only nadal warto zachować inny model dla Reviewera. Jeżeli sprzęt nie mieści `laguna-s-2.1`,
wybierz mniejszy model z `tools` i najlepiej `thinking`, a następnie zwiększ znaczenie deterministic
verifiers i nie włączaj automerge przed walidacją E2E na danym stacku.

## Jak zmienić model

Najpierw zobacz dokładne ID widoczne przez skonfigurowany OpenWebUI:

```bash
converge models --config /workspace/my-project/converge.yaml
```

Następnie zmień tylko `models.profiles.<role>.model`. Nie edytuj generowanego
`<state_dir>/opencode.generated.json`.

Po zmianie zawsze uruchom:

```bash
converge doctor --config /workspace/my-project/converge.yaml
```

`doctor` sprawdza, czy wszystkie modele aktywnych agentów są faktycznie widoczne przez gateway.

## Parametry modelu

`request_body` jest dostępny dla świadomych, provider-specific override'ów:

```yaml
models:
  profiles:
    builder:
      model: kimi-k2.7-code:cloud
      request_body:
        temperature: 1.0
        top_p: 0.95
```

Nie jest to jednak domyślna polityka Converge. Wartość należy ustawiać dopiero po benchmarku na
konkretnym repo i przez ten sam OpenWebUI/OpenCode transport. Zmiana parametrów nie może wpływać na
integracyjne reguły bezpieczeństwa: testy, mandatory regression gate, independent review i CI są
nadrzędne.

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
