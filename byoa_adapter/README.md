# BYOA Adapter

Normalizes external Agent Cards and translates JSON-RPC `tasks.execute` calls into AQE workflow, validation, and repair operations.

**Contracts:** `qe.run`, `qe.validate`, `qe.repair` · port `8009`.

```mermaid
flowchart LR
  Client[External A2A client] --> Adapter[BYOA Adapter]
  Adapter -->|qe.run| Temporal[Workflow API]
  Adapter -->|qe.validate / qe.repair| Executor[Test Execution]
  Temporal --> Adapter --> Client
```

The adapter is a protocol boundary, not a credential store. Target authentication is supplied through runtime policy.
