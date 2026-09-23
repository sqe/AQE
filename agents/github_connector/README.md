# GitHub Connector Agent

Provides an allowlisted boundary around GitHub's MCP server for source discovery and safe tool calls.

**Contracts:** `github.tools.list`, `github.tools.call` · port `8014`.

```mermaid
flowchart LR
  AQE[AQE callers] --> Policy[Tool + repository policy]
  Policy --> Connector[GitHub Connector]
  Connector --> MCP[GitHub MCP]
  MCP --> Connector --> AQE
```

The Agent Card declares executable list and call scenarios. Tokens stay in the connector; generated tests receive no GitHub credential.
