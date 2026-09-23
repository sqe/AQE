# dbt Builder Agent

Builds and validates review-only dbt projects with staging, intermediate, marts, documentation, and data-test coverage.

**Contracts:** `dbt.blueprint.build`, `dbt.project.validate` · port `8016`.

```mermaid
flowchart LR
  Goals[Analytics goals + sources] --> Builder[dbt Builder]
  Builder --> Layers[Staging → Intermediate → Marts]
  Layers --> Validate[SQL/YAML/docs/tests validation]
  Validate --> Bundle[(Review-only project bundle)]
```

The Agent Card contains executable examples. Generated projects are never deployed automatically.
