# Change Detection Agent

Consumes change signals, captures the appropriate target context, and dispatches the correct AQE generation and execution path.

**Contract:** `detect_change_and_orchestrate` · `POST /detect_changes` · port `8000`.

```mermaid
flowchart LR
  Change[Change event] --> Detect[Change Detection]
  Detect -->|website| Capture[Webpage State Capture]
  Detect -->|agent| Generate[Test Generation]
  Capture --> Generate
  Generate --> Execute[Test Execution]
```

Dependencies: Kafka, Redis, generation, state capture, and execution. Positive smoke tests create workflows and therefore belong in an isolated candidate environment.
