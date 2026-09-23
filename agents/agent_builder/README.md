# Agent Builder

Creates review-only agent source bundles from requirements and pinned evidence. Generated bundles are validated and stored for human review; this agent never deploys them.

**Contract:** `agent.build.experimental` · `POST /v1/builds` · port `8015`.

```mermaid
flowchart LR
  Spec[Specification + evidence] --> Builder[Agent Builder]
  Builder --> Validate[Static contract validation]
  Validate --> RustFS[(RustFS bundle)]
  Validate --> Review[Human review required]
```

Dependencies: source analysis, generation model, and RustFS. See the root README for runtime secrets and the production release guide for promotion policy.
