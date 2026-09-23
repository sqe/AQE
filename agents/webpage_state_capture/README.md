# Webpage State Capture Agent

Captures browser-observable page state for website test generation and stores bounded context in Redis.

**Contract:** `capture_state` · `POST /capture` · port `8002`.

```mermaid
flowchart LR
  URL[Target URL] --> Browser[Playwright capture]
  Browser --> State[DOM + page state]
  State --> Redis[(Redis context)]
  State --> Generation[Website test generation]
```

Navigation is network-active. Allow only reviewed targets and use disposable candidate environments for smoke tests.
