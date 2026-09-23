# Agent Test Execution

Runs quality-gated HTTP/A2A pytest suites in a bounded sandbox, persists JUnit evidence, and catalogs passing suites.

**Contracts:** `qe.validate`, `qe.repair` · `POST /run_tests` · port `8003`.

```mermaid
flowchart LR
  Suite[(RustFS test suite)] --> Gate[Static quality gate]
  Gate --> Sandbox[Bounded pytest sandbox]
  Sandbox --> Results[(PostgreSQL + JUnit)]
  Results -->|pass| Catalog[Generated-test catalog]
  Results -->|eligible failure| Repair[One bounded repair]
```

This image intentionally contains no browser. Generated tests must declare their AQE layer and semantic suite identity.
