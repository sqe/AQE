# AQE — Agentic Quality Engineering

AQE is a **compound agent system** that turns product context and observed UI
state into generated tests, executes them in a bounded sandbox, diagnoses
failures, and optionally repairs and re-runs the tests. The public role is the
**QE orchestrator**; “umbrella agent” and “thick agent” are understandable but
are not standard architecture terms.

See [Architecture and operating rules](docs/architecture.md) for the complete
Temporal rules, Mermaid flow diagrams, deployment procedures, BYOA protocol,
and internal/external agent examples.

## Architecture

```text
Client / agentic-kubernetes-platform supervisor
                    │ JSON-RPC over Kafka
                    ▼
            AQE BYOA adapter
                    │
                    ▼
             Temporal workflow
       ┌────────────┴────────────┐
       ▼                         ▼
Test generation             Test execution
RAG + model                  quality gate
       │                     sandbox + pytest
       │                         │
       │                    failure diagnosis
       │                         │
       └──────────────────► bounded repair loop
                                 │
                                 ▼
                    PostgreSQL + RustFS evidence
```

### Ownership boundaries

| Layer | Responsibility |
|---|---|
| BYOA adapter | Platform Agent Card, registry refresh, `tasks.aqe` / `results.aqe`, result-before-offset-commit |
| Temporal | Durable generate → execute → repair coordination |
| Generation agent | Product RAG + agent-ontology-grounded pytest source and immutable test artifact |
| Agent executor | Browser-free HTTP/Agent Card/A2A tests in an isolated bounded runtime |
| Website executor | Playwright Chromium journeys in a separate browser runtime |
| Repair client | At most `MAX_REPAIR_ATTEMPTS`; preserve expected behavior and never weaken assertions |
| Diagnostics agent | Fleet Agent Card checks, skill contract probes, safe retries, and recovery recommendations |
| PostgreSQL / RustFS | Test-run state, source, repaired artifacts, output, and audit evidence |

Generated code is never silently changed before its first run. The old executor
rewrote URLs, expected strings, and locator strictness, which could manufacture
false passes; that behavior has been removed.

## Test quality policy

Every generated test must:

- verify one observable outcome with one assertion or Playwright `expect`;
- use fixtures for shared setup and cleanup;
- use role/label/test-id locators and explicit state waits;
- remain independent of execution order;
- preserve expected values during repair;
- avoid fixed sleeps, swallowed failures, and conditional assertion skipping.

The AST quality gate enforces the atomic-outcome rules before execution. Pytest
exit status and JUnit XML—not output-string matching—determine the result.

When `TEST_CATALOG_REPOSITORY` and `TEST_CATALOG_GITHUB_TOKEN` are configured,
execution commits immutable Python tests and metadata to
`generated-tests/<test-type>/<agent>/<version>/` on `TEST_CATALOG_BRANCH`. This lets CI test
specific versions of teaching, listening, finance, insurance, or any other
agent without encoding profession-specific behavior into AQE. Keep the catalog
repository private when scenarios or expected outcomes contain sensitive data.
When the catalog is this repository, `.github/workflows/generated-tests.yml`
runs the generated branch automatically with read-only GitHub permissions.

Confirmed product defects are also durable GitHub artifacts, but ordinary test
failures are not. After triage, `POST /v1/findings/<task-id>/confirm` reruns the
test without repair and requires non-empty evidence. Only assertion failures
with zero execution/collection errors are published to
`generated-findings/<test-type>/<agent>/<version>/`. CI verifies each finding
continues to reproduce; a newly passing reproducer signals that the defect was
fixed and the catalog should be updated.

## Local development

```bash
docker compose up --build
```

Endpoints:

- dashboard: <http://localhost:3000>
- execution: <http://localhost:8003/health>
- website execution: <http://localhost:8013/health>
- durable workflow API: <http://localhost:8008/docs>
- BYOA discovery: <http://localhost:8009/.well-known/agent.json>
- diagnostics and external agent probes: <http://localhost:8006/docs>
- Temporal UI: <http://localhost:8233>

Start a durable run:

```bash
curl -s http://localhost:8008/v1/qe-runs \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","test_type":"website","spec":"Verify the primary user flow","repair":true}'
```

To ground agent testing in its implementation, configure a read-only fine-grained
GitHub token and an explicit repository allowlist, then include a commit, tag,
or version ref in the run:

```bash
export GITHUB_SOURCE_ALLOWED_REPOSITORIES=sqe/example-agent
export GITHUB_SOURCE_TOKEN=github_pat_read_only
curl -s http://localhost:8008/v1/qe-runs \
  -H 'content-type: application/json' \
  -d '{"url":"https://agent.example/rpc","test_type":"agent","source_repository":"sqe/example-agent","source_ref":"a1b2c3d","spec":"Validate declared skills"}'
```

The source-analysis agent reads at most 30 allowlisted source files and 500 KB,
records blob/tree SHAs, and emits candidate line-level findings. The generation
model uses those candidates to design black-box reproductions. They do not
become confirmed defects until execution and the explicit finding gate pass.

Configure repair with an OpenAI-compatible chat-completions endpoint:

```bash
export REPAIR_LLM_URL=http://host.docker.internal:8081/v1/chat/completions
export REPAIR_LLM_MODEL=your-model
```

## agentic-kubernetes-platform BYOA

Yes—AQE can be brought into the platform as one compound specialist. Do not
expose every internal worker as a platform agent. Configure the adapter to use
the platform registry and Kafka:

```bash
export PLATFORM_REGISTRY_URL=http://platform-agentic-platform-registry.agentic-platform.svc:8001
export PLATFORM_KAFKA_BOOTSTRAP_SERVERS=kafka-kafka-bootstrap.messaging.svc:9092
```

Published skills are:

- `qe.run`: start the durable end-to-end workflow;
- `qe.validate`: run a persisted test without repair;
- `qe.repair`: diagnose, repair, and re-run a persisted test.

The adapter accepts native platform JSON-RPC and Model Fleet's `tasks.execute`
envelope, publishes correlated results, and commits Kafka input only after the
result is published.

## Agentic QE automata and agent contract testing

“Agentic QE automaton” is a useful product name for AQE: the standard technical
description remains a **compound agent system** with a durable workflow/state
machine. The diagnostics agent validates all internal Agent Cards after every
Argo CD sync and supports contract probes for known agents on internal or
external networks:

```bash
curl -s http://localhost:8006/v1/agent-probes \
  -H 'content-type: application/json' \
  -d '{"card_url":"https://agent.example/.well-known/agent.json","expected_skills":["research.run"]}'
```

External hosts must be explicitly added to `AGENT_PROBE_ALLOWED_HOSTS` (or the
Helm `config.agentProbeAllowedHosts` value). An optional `invocation` object can
exercise a JSON-RPC scenario after its Agent Card and skills pass. “Safe heal”
retries transient checks and returns remediation guidance; it intentionally
does not restart workloads or mutate clusters without a separate authorized
platform operation.

## Continuous evaluation

Golden generation and repair cases live in `evaluation/golden/*.jsonl`.
Positive and adversarial negative examples check atomic tests, stable waiting,
and preservation of expected values.

```bash
python evaluation/run.py --minimum-score 1.0
EVAL_MODEL_URL=https://model.example/v1/chat/completions \
  EVAL_MODEL_NAME=my-model python evaluation/run.py --live --minimum-score 0.8
```

CI validates the dataset on every change. A scheduled workflow evaluates a live
model when `EVAL_MODEL_URL` and optional API-key secrets are configured.

## Agent ontology and live graph

The versioned ontology at `ontology/agent_ontology.json` describes agent
interfaces, autonomy, sensitivity, impact, archetypes, and risk overlays. The
knowledge-ingestion agent stores it in Qdrant and RustFS without deleting
product knowledge. The generator combines deterministic applicable ontology
rules with retrieved product evidence, so teaching, listening, finance,
insurance, healthcare, action, research, and orchestration agents receive
different obligations without hard-coded test implementations.

The dashboard's live graph follows the neighboring platform knowledge-graph
visualizer contract (`nodes`, `edges`, `stats`). It displays AQE agents,
infrastructure, discovered internal/external targets, versions, skills, and
ontology classes. Every generation emits a test node and animated routing edges
to the correct HTTP or browser executor.

## CI/CD and Argo CD

- `.github/workflows/ci.yml`: compile, unit tests, golden evaluation, Compose
  validation, both Helm profiles, every independently owned image, and an execution E2E
  test with real PostgreSQL and RustFS persistence.
- `.github/workflows/release.yml`: builds and publishes versioned GHCR images and
  packages the Helm chart; published images include provenance and SBOM
  attestations.
- `deploy/argocd/project.yaml` and `application.yaml`: scoped source/destination,
  automated prune/self-heal, retry policy, server-side apply, and independent
  Argo CD Image Updater digest tracking for every agent.
- `deploy/helm/aqe/templates/preflight.yaml`: a PreSync diagnostic hook that
  blocks rollout when PostgreSQL, RustFS, Kafka, Temporal, or the configured model
  endpoint is unavailable, plus a PostSync check for every AQE Agent Card.

Every deployable agent owns `agents/<agent>/{app.py,Dockerfile,requirements.txt,agent.yaml}`.
The two execution agents are distinct image and policy boundaries: HTTP/A2A
tests do not carry a browser, while website tests use the official Playwright
runtime. Install the Argo CD project and application after providing
`aqe-runtime-secrets` through your secret-management system:

```bash
kubectl apply -f deploy/argocd/project.yaml -f deploy/argocd/application.yaml
```

The application selects `values-agentic-platform.yaml`, which uses the
neighboring platform's `agentic-platform`, `messaging`, `temporal`, and `rustfs`
service DNS names. Local rendering does not mutate that cluster.

`aqe-runtime-secrets` must be created by the cluster's secret manager. At
minimum it supplies `POSTGRES_URL`, RustFS-compatible `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, and `QDRANT_API_KEY`. Add scoped `GITHUB_PAT`,
`GITHUB_SOURCE_TOKEN`, or `TEST_CATALOG_GITHUB_TOKEN` only when those GitHub
capabilities are enabled; none belong in Helm values.

Never commit model keys, database passwords, or registry credentials. The chart
references an optional Secret and keeps credentials out of the repository.

## Verification

```bash
python -m pytest -q test/test_quality.py test/test_byoa_adapter.py test/test_evaluation.py
python evaluation/run.py --minimum-score 1.0
helm lint deploy/helm/aqe
helm template aqe deploy/helm/aqe >/dev/null
docker compose config --quiet
test/e2e/run.sh
```

For production, run generated tests in one disposable Kubernetes Job per attempt
with a read-only root filesystem, seccomp, no service-account token, strict
resource quotas, and an egress policy limited to the system under test. The
current Compose subprocess sandbox is bounded and secret-scrubbed, but a
container is the local security boundary—not the Python subprocess alone.
