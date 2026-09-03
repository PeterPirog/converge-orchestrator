# Migracja do parallel review fan-out

Ten dokument jest krótką notą dla istniejących instalacji Converge. Pełny nowy projekt powinien
kopiować [`examples/converge.yaml`](../examples/converge.yaml).

## Istniejąca konfiguracja

Stare projekty z pojedynczym:

```yaml
agents:
  reviewer:
    agent: converge-reviewer
    model_profile: reviewer
```

pozostają kompatybilne. Jeżeli `workflow.review_roles` nie istnieje, `review` zachowuje dotychczasowe
pojedyncze wywołanie.

## Zalecany upgrade

Dodaj profile/agentów dla trzech read-only lane'ów i wskaż je w `workflow`:

```yaml
agents:
  correctness_reviewer:
    agent: converge-correctness-reviewer
    model_profile: reviewer
    timeout_seconds: 1800
    steps: 24

  architecture_reviewer:
    agent: converge-architecture-reviewer
    model_profile: planner
    timeout_seconds: 1800
    steps: 24

  security_reviewer:
    agent: converge-security-reviewer
    model_profile: security
    timeout_seconds: 1800
    steps: 24

workflow:
  review_roles:
    - correctness_reviewer
    - architecture_reviewer
    - security_reviewer
  max_parallel_reviews: 3
```

Dla referencyjnego OpenWebUI katalogu `security` używa `gpt-oss:120b`. Jeżeli lokalny sprzęt nie może
obsłużyć kilku ciężkich modeli jednocześnie, zachowaj wszystkie lane'y i zmniejsz
`max_parallel_reviews` do `1` lub `2`.

Po zmianie uruchom:

```bash
converge models --config /path/to/converge.yaml
converge doctor --config /path/to/converge.yaml
```

Nie trzeba zmieniać LangGraph workflow ani target repository.
