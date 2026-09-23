# Diagnostics Agent

Discovers Agent Cards, validates declared skills, classifies fleet capabilities, records live graph events, and reports safe remediation guidance.

**Contracts:** `diagnostics.scan`, `diagnostics.probe`, `diagnostics.heal` · port `8006`.

```mermaid
flowchart LR
  Cards[Configured Agent Cards] --> Scan[Diagnostics]
  Scan --> Contracts[Contract and ontology results]
  Events[Workflow events] --> Graph[Live topology]
  Scan --> Graph
  Scan --> Health[Model + fleet health]
```

External probes are restricted by `AGENT_PROBE_ALLOWED_HOSTS`; diagnostics never turns an undeclared contract into a pass.
