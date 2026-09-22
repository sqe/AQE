# AQE agents

Each directory is an independently versioned deployment boundary and owns its
entrypoint, dependencies, container image, and `agent.yaml` contract. Shared
protocol/storage code remains in `utils/`; control-plane services remain outside
this directory.

Image names match directory names: `ghcr.io/sqe/aqe-<directory>:<version>`.
The agent-test and website-test execution images intentionally share execution
logic, but the website image alone contains Playwright and Chromium.
