# AQE Automata Architecture and Operating Rules

This document is the authoritative design reference for AQE. AQE is a
**compound agent system** exposed to a larger platform as one quality-engineering
specialist. “AQE Automata” is the product name for the durable state machine
that generates, executes, diagnoses, repairs, evaluates, and catalogs tests.

## 1. System context

```mermaid
flowchart LR
    Operator[Operator or CI] --> UI[AQE terminal UI]
    Platform[agentic-kubernetes-platform] -->|JSON-RPC / Kafka| BYOA[BYOA adapter]
    UI --> WorkflowAPI[Workflow API]
    BYOA --> WorkflowAPI
    WorkflowAPI --> Temporal[(Temporal)]
    Temporal --> Generator[Test generation agent]
    Temporal --> AgentExecutor[HTTP agent-test executor]
    Temporal --> WebExecutor[Playwright website-test executor]
    Diagnostics[Diagnostics agent] --> Generator
    Diagnostics --> AgentExecutor
    Diagnostics --> WebExecutor
    Diagnostics --> Internal[Other internal agents]
    Diagnostics --> External[Allowed external agents]
    Generator --> Qdrant[(Qdrant knowledge)]
    Generator --> RustFS[(RustFS objects)]
    AgentExecutor --> RustFS
    WebExecutor --> RustFS
    Generator --> PostgreSQL[(PostgreSQL state)]
    AgentExecutor --> PostgreSQL
    WebExecutor --> PostgreSQL
    AgentExecutor --> Catalog[Versioned GitHub test catalog]
    WebExecutor --> Catalog
```

### Ownership rules

| Component | Owns | Must not own |
|---|---|---|
| BYOA adapter | Platform discovery, Kafka envelope normalization, correlated result publication | Test generation or execution logic |
| Temporal workflow | Durable ordering, activity timeouts, retries, workflow identity | Test source or infrastructure credentials |
| Generation agent | RAG grounding and domain-neutral Python test generation | Silently changing results after execution |
| Agent executor | Browser-free HTTP/Agent Card/A2A tests, static gate, bounded pytest, repair | Installing or invoking browser tooling |
| Website executor | Playwright browser journeys, static gate, bounded pytest, repair | Agent protocol tests that belong in the HTTP runtime |
| Diagnostics agent | Agent Card/skill contract checks, safe retry, recommendations | Unapproved restarts or cluster mutation |
| PostgreSQL | Mutable run state and queryable audit metadata | Large source or evidence blobs |
| RustFS | Generated/repaired source and large evidence | Workflow state transitions |
| GitHub catalog | Validated, passing tests grouped by agent version | Secrets or unvalidated model output |

### Repository and deployment boundaries

Every box under `agents/` is independently buildable and has one checked-in
`agent.yaml` contract. Shared utilities are copied only into images that use
them. Temporal, BYOA, reporting, and the frontend are control-plane components,
not mislabeled internal agents.

```mermaid
flowchart TB
    subgraph Repository[AQE repository]
        subgraph Agents[agents/ — independent image boundaries]
            A[artifact_management]
            C[change_detection]
            D[diagnostics]
            GA[github_analysis]
            GC[github_commit]
            K[knowledge_ingestion]
            TG[test_generation]
            TE[test_execution — HTTP only]
            WS[webpage_state_capture — Playwright]
            WE[website_execution — Playwright]
        end
        Shared[utils/ — shared protocols, auth, storage]
        Control[Temporal / BYOA / reporting / frontend]
        Chart[Helm chart + platform profile]
        GitOps[Argo CD AppProject + Application]
    end
    Shared --> A
    Shared --> K
    Shared --> TG
    Shared --> TE
    Shared --> WE
    Agents --> Chart
    Control --> Chart
    Chart --> GitOps
    GitOps --> Cluster[agentic-kubernetes-platform cluster]
```

The default NetworkPolicy permits AQE-internal calls, DNS, explicit platform
dependency ports, Kafka in `messaging`, Temporal/RustFS service ports, and HTTPS
for allowlisted external targets. Pods run without service-account tokens, as a
non-root user, with `RuntimeDefault` seccomp, dropped capabilities, read-only
root filesystems, and bounded temporary volumes where execution requires them.

## 2. End-to-end QE automaton

```mermaid
sequenceDiagram
    autonumber
    actor Caller
    participant API as Workflow API
    participant T as Temporal
    participant G as Generation agent
    participant R as RustFS
    participant DB as PostgreSQL
    participant E as Execution agent
    participant M as Repair model
    participant GH as GitHub catalog

    Caller->>API: POST /v1/qe-runs\nURL, observable behavior, target agent/version
    API->>T: Start workflow with unique workflow_id
    T->>G: generate_tests activity
    G->>G: Retrieve versioned RAG context
    G->>G: Generate domain-neutral pytest source
    G->>R: Store immutable candidate source
    G->>DB: Insert PENDING run + target agent metadata
    G-->>T: task_id
    T->>E: execute_and_repair activity(task_id)
    E->>DB: Resolve RustFS path and target version
    E->>R: Read candidate source
    E->>E: AST quality gate
    E->>E: Execute pytest in bounded workspace
    alt passing result
        E->>GH: Publish Python test + metadata when configured
        E->>DB: Persist PASSED, JUnit summary, catalog link
    else failed and repair is enabled
        E->>M: Failure evidence + source + non-weakening rules
        M-->>E: Candidate repaired source
        E->>E: Repeat quality gate and execution
        E->>R: Store final repaired source
        E->>DB: Persist final result and all attempts
    end
    E-->>T: Final structured result
    T-->>API: Workflow result
    API-->>Caller: Queryable workflow status/result
```

## 3. Temporal rules

Temporal is the durable coordinator, not a replacement for agent logic.

| Rule | Implemented behavior | Reason |
|---|---|---|
| Workflow identity | Caller may supply `workflow_id`; otherwise the API creates `qe-<uuid>` | Enables idempotent caller-controlled submission |
| Generation retries | `maximum_attempts=1`, three-minute timeout | Generation persists a new artifact and is not idempotent yet |
| Execution retries | `maximum_attempts=2`, fifteen-minute timeout, two-minute heartbeat timeout | A transient dispatch failure may recover; execution state remains keyed by `task_id` |
| Repair attempts | Execution agent allows `MAX_REPAIR_ATTEMPTS` (default 2) inside one activity | Repair needs complete prior-attempt evidence and a strict bound |
| Activity boundary | Network and database work lives in activities, never workflow code | Keeps workflow replay deterministic |
| Payload rule | Workflow state carries IDs and small JSON; source remains in RustFS | Avoids oversized Temporal histories |
| Cancellation | Worker cancellation stops orchestration; execution subprocess has its own timeout and limits | Durable cancellation and sandbox control are separate concerns |
| Future idempotency | Add a generation idempotency key before enabling generation activity retries | Prevents duplicate artifacts and catalog entries |

```mermaid
stateDiagram-v2
    [*] --> Accepted
    Accepted --> Generating
    Generating --> GenerationFailed: non-retryable generation error
    Generating --> Executing: task_id persisted
    Executing --> Passed: pytest + quality gate pass
    Executing --> Diagnosing: failure and repair enabled
    Diagnosing --> Executing: repaired candidate / attempts remain
    Diagnosing --> Failed: no candidate or repair limit reached
    Executing --> Failed: repair disabled or limit reached
    Passed --> Cataloged: GitHub catalog configured
    Passed --> Completed: catalog disabled
    Cataloged --> Completed
    GenerationFailed --> [*]
    Failed --> [*]
    Completed --> [*]
```

## 4. Generated-test rules

1. Tests are executable Python `pytest` files.
2. `test_type=website` routes browser journeys to the Playwright image;
   `test_type=agent` routes Agent Card, A2A, JSON-RPC, API, and orchestration
   tests to a browser-free Python/httpx image. The runtimes reject mismatched jobs.
3. Every `test_` function verifies one observable outcome with one Python
   `assert` or Playwright `expect`.
4. Shared setup belongs in fixtures; tests do not rely on execution order.
5. Fixed sleeps, swallowed assertion failures, conditional assertion skipping,
   and weakened expected values are forbidden.
6. Expected behavior comes from the supplied card, skills, scenario, product
   evidence, and golden data—not from a hard-coded profession.
7. Passing source is committed to the versioned GitHub green catalog only after
   the static gate and runtime execution pass.
8. A failed test enters the GitHub findings catalog only after explicit defect
   confirmation with evidence and a clean assertion-only reproduction. Quality,
   collection, environment, and runtime errors are never product findings.

```mermaid
flowchart TD
    Candidate[Generated Python candidate] --> Parse{Valid Python?}
    Parse -- No --> Reject[Reject with quality issue]
    Parse -- Yes --> Tests{Has test functions?}
    Tests -- No --> Reject
    Tests -- Yes --> Atomic{Exactly one observable assertion per test?}
    Atomic -- No --> Reject
    Atomic -- Yes --> Sandbox[Bounded pytest workspace]
    Sandbox --> Result{Exit code and JUnit pass?}
    Result -- No --> Repair{Repair enabled and attempts left?}
    Repair -- Yes --> Candidate
    Repair -- No --> Evidence[Persist failure evidence]
    Result -- Yes --> Persist[Persist validated source and result]
    Persist --> Catalog{GitHub catalog configured?}
    Catalog -- Yes --> VersionPath[generated-tests/test-type/agent/version/test_task.py]
    Catalog -- No --> Done[Complete]
    VersionPath --> Done
```

```mermaid
flowchart TD
    Failed[Generated test failed] --> Triage{Disposition}
    Triage -- test defect / environment / untriaged --> EvidenceOnly[Keep evidence in PostgreSQL + RustFS]
    Triage -- confirmed product defect --> Confirm[POST findings/task-id/confirm with evidence]
    Confirm --> Rerun[Re-run without model repair]
    Rerun --> Assertion{At least one assertion failed and zero execution errors?}
    Assertion -- No --> RejectFinding[Reject GitHub finding publication]
    Assertion -- Yes --> FindingCatalog[generated-findings/type/agent/version/test.py + metadata]
    FindingCatalog --> FindingCI[CI expects pytest assertion failure]
    FindingCI -->|still fails| Open[Finding remains reproducible]
    FindingCI -->|passes| Resolved[Signal defect resolved; update catalog]
```

### Runtime and authentication boundary

```mermaid
flowchart LR
    Request[Run request] --> Type{test_type}
    Type -- agent --> HTTP[Clean Python + pytest + httpx]
    Type -- website --> Browser[Python + pytest + Playwright Chromium]
    HTTP --> AgentAuth[Bearer / basic / API key / OAuth2 client credentials]
    Browser --> WebAuth[HTTP auth / headers / explicit form-login selectors]
    AgentAuth --> TargetAgent[Agent Card and skill endpoint]
    WebAuth --> TargetSite[Target website]
    Secrets[Only TARGET_AUTH_* credentials] --> AgentAuth
    Secrets --> WebAuth
    PlatformSecrets[AQE DB, RustFS, model and GitHub secrets] -. never forwarded .-> HTTP
    PlatformSecrets -. never forwarded .-> Browser
```

Generated tests run with only explicitly allowlisted target variables. In
GitHub, both catalogs use the protected `generated-agent-e2e` Environment so a
repository administrator can require approval before target credentials reach
generated code.

## 5. Agent ontology and RAG grounding

`ontology/agent_ontology.json` is a versioned source of truth. It classifies
agents across interaction style, interface, autonomy, sensitivity, and impact;
defines universal obligations; and supplies archetype/risk-specific scenarios.
Initial archetypes cover conversational, teaching/coaching,
listening/transcription, finance, insurance, healthcare, retrieval/research,
side-effecting action, multi-agent orchestration, and website/browser systems.

```mermaid
flowchart TD
    Source[Versioned ontology JSON] --> Validate[Schema and unique-ID validation]
    Validate --> Flatten[Universal + archetype + risk-overlay records]
    Flatten --> Embed[Knowledge-ingestion agent embeds records]
    Embed --> Qdrant[(Qdrant product_knowledge collection)]
    Validate --> RustFS[(RustFS immutable ontology artifact)]
    RustFS --> Registry[(PostgreSQL AGENT_ONTOLOGY active version)]
    Card[Agent Card, skills, scenario, network and risk labels] --> Select[Deterministic ontology selection]
    Qdrant --> Retrieve[Semantic product and ontology evidence]
    Select --> Prompt[Grounded generation prompt]
    Retrieve --> Prompt
    Prompt --> Tests[Atomic, archetype-aware generated tests]
```

Product ingestion replaces only `source=product_knowledge` vectors; ontology
ingestion replaces only `source=agent_ontology`. It never recreates the shared
collection. Argo CD runs ontology ingestion after sync, and the generator also
loads applicable ontology rules deterministically so test obligations are not
left solely to model retrieval quality.

### GitHub source-analysis evidence

```mermaid
sequenceDiagram
    participant T as Temporal
    participant S as GitHub source-analysis agent
    participant GH as GitHub API
    participant G as Test generator + LLM
    participant E as Isolated executor
    T->>S: repository + pinned ref + optional paths
    S->>S: Enforce exact repository allowlist and byte/file limits
    S->>GH: Read tree and source blobs with scoped read-only token
    GH-->>S: Source + immutable tree/blob SHAs
    S->>S: Produce line-level candidate findings
    S-->>T: Candidate evidence, provenance, and limits
    T->>G: Scenario + ontology + product RAG + source evidence
    G->>G: Design observable reproduction tests
    G-->>E: Generated test artifact
    E->>E: Runtime reproduction and quality gate
```

Source inspection is read-only and opt-in. `GITHUB_SOURCE_ALLOWED_REPOSITORIES`
must contain exact `owner/name` entries; `GITHUB_SOURCE_TOKEN` should be a
fine-grained contents-read token. AQE never calls source evidence a product
defect by itself: candidates must become observable tests and pass the confirmed
finding publication gate.

## 6. Internal and external agent validation

The diagnostics agent accepts only `http` or `https` URLs without embedded
credentials. Hosts must be present in `AGENT_PROBE_ALLOWED_HOSTS`; this prevents
the probe API from becoming an unrestricted server-side request primitive.

```mermaid
sequenceDiagram
    autonumber
    actor QE as QE operator
    participant D as Diagnostics agent
    participant A as Internal or external agent

    QE->>D: POST /v1/agent-probes\ncard_url + expected_skills
    D->>D: Validate scheme, host allowlist, and request shape
    D->>A: GET Agent Card
    A-->>D: identity, version, skills, endpoint
    D->>D: Compare declared and expected skills
    alt Contract-only probe
        D-->>QE: healthy/degraded + missing skills + recommendation
    else Optional orchestration scenario
        D->>A: JSON-RPC invocation
        A-->>D: correlated result or error
        D-->>QE: contract result + scenario evidence
    end
```

### Internal AQE fleet scan

```json
{
  "artifact-management": "http://aqe-artifact-management:8007/agent_card",
  "test-generation": "http://aqe-test-generation:8001/agent_card",
  "test-execution": "http://aqe-test-execution:8003/agent_card",
  "website-execution": "http://aqe-website-execution:8013/agent_card",
  "aqe-byoa": "http://aqe-byoa:8009/.well-known/agent.json"
}
```

Argo CD runs infrastructure checks as a `PreSync` hook and Agent Card checks as
a `PostSync` hook. The long-running diagnostics agent powers the UI and returns
recommendations. Its safe-heal action retries failed checks once; workload
restarts require a separate, explicitly authorized platform operation.

### External teaching-agent example

The same contract works for teaching, listening, finance, insurance, or any
other profession. The domain appears in the target agent's contract—not in AQE
code.

```json
{
  "name": "algebra-tutor",
  "card_url": "https://agents.school.example/.well-known/agent.json",
  "expected_skills": ["lesson.explain", "assessment.grade"],
  "invocation": {
    "url": "https://agents.school.example/rpc",
    "method": "tasks.execute",
    "params": {
      "skill": "lesson.explain",
      "context": {
        "learner_level": "grade-8",
        "topic": "linear equations",
        "observable_outcome": "explanation includes one worked example and a check-for-understanding"
      }
    }
  }
}
```

For versioned generation, pass the discovered identity alongside the scenario:

```json
{
  "url": "https://agents.school.example/rpc",
  "agent_card_url": "https://agents.school.example/.well-known/agent.json",
  "target_agent": {"id": "algebra-tutor", "version": "3.4.1"},
  "skills": ["lesson.explain", "assessment.grade"],
  "agent_archetype": "teaching_coaching",
  "network_scope": "external",
  "risk_labels": ["personal", "moderate"],
  "test_type": "agent",
  "spec": "Verify a grade-8 linear-equation explanation returns one worked example and one independent check-for-understanding.",
  "repair": true
}
```

After a passing run, AQE publishes:

```text
generated-tests/agent/algebra-tutor/3.4.1/test_<task-id>.py
generated-tests/agent/algebra-tutor/3.4.1/test_<task-id>.json
```

## 7. Live topology and test graph

The diagnostics agent exposes `GET /v1/graph` in the same `{nodes, edges,
stats}` shape used by the platform knowledge-graph visualizer. It merges live
fleet scans, discovered external agents, infrastructure, and recent generation
events. The terminal UI polls every five seconds, animates newly generated test
edges, and shows identity, version, skills, ontology archetype, and status on
selection.

```mermaid
sequenceDiagram
    participant UI as Terminal graph view
    participant D as Diagnostics graph API
    participant G as Test generator
    participant T as Target agent
    UI->>D: GET /v1/graph every 5s
    D->>D: Merge fleet scan + observed targets + recent events
    D-->>UI: nodes, edges, stats, events
    G->>D: POST test_generated(task_id, type, target, archetype)
    D->>D: Add generated-test node and routing edges
    UI->>D: Next graph poll
    D-->>UI: New test → executor → target interaction
    UI->>UI: Animate live edge and expose node details
    D->>T: Agent Card probe
    D->>D: Add/update external target node
```

Recent generation events are intentionally bounded in memory. PostgreSQL and
RustFS remain the durable audit sources; a future high-volume graph can project
the same events into a dedicated graph store without changing the API.

## 8. BYOA platform flow

```mermaid
sequenceDiagram
    participant Registry as Platform registry
    participant AQE as AQE BYOA adapter
    participant Kafka
    participant Temporal

    loop Registration refresh
        AQE->>Registry: Register card and qe.run/qe.validate/qe.repair skills
    end
    Kafka->>AQE: tasks.aqe JSON-RPC envelope
    AQE->>AQE: Normalize native or tasks.execute envelope
    AQE->>Temporal: Start run or dispatch persisted validation
    Temporal-->>AQE: Accepted/result
    AQE->>Kafka: Publish correlated results.aqe message
    AQE->>Kafka: Commit input offset only after result publication
```

## 9. CI, continuous testing, CD, and GitOps

```mermaid
flowchart LR
    PR[Pull request] --> Unit[Compile + unit tests]
    PR --> Eval[Golden model evaluation]
    PR --> Manifests[Compose + Helm validation]
    PR --> Images[Build every agent and component image]
    PR --> E2E[Real RustFS/PostgreSQL execution E2E]
    Unit --> Gate{All required checks pass?}
    Eval --> Gate
    Manifests --> Gate
    Images --> Gate
    E2E --> Gate
    Gate -- Yes, merge to main --> Release[Publish GHCR images + provenance + SBOM]
    Release --> Argo[Argo CD image update]
    Argo --> PreSync[PreSync dependency diagnostics]
    PreSync --> Deploy[Server-side apply]
    Deploy --> PostSync[PostSync Agent Card validation]
    PostSync --> Ready[Healthy AQE automaton]
    CatalogPush[Validated test catalog push] --> VersionCI[Run generated Python tests for agent version]
```

### Deployment invariants

- The official `rustfs/rustfs` image provides S3-compatible object storage.
- Runtime secrets come from `aqe-runtime-secrets`; they are not committed.
- Argo CD uses automated prune/self-heal and server-side apply.
- PreSync diagnostics must pass before workloads update.
- PostSync Agent Card validation must pass before the sync is considered healthy.
- External probe hosts are allowlisted per environment.
- Publishing tests requires a scoped GitHub token with contents write access to
  the selected catalog repository. Generated-test CI has read-only repository
  permissions; target credentials are available only through the protected
  `generated-agent-e2e` Environment when its secrets are configured.

## 10. Failure and recovery procedure

```mermaid
flowchart TD
    Alert[Failed workflow or degraded agent] --> Scan[Diagnostics fleet scan]
    Scan --> Transient{Endpoint recovers on safe retry?}
    Transient -- Yes --> Resume[Resume or re-run durable workflow]
    Transient -- No --> Contract{Agent Card or skill mismatch?}
    Contract -- Yes --> Recommend[Report exact missing identity/skill and version evidence]
    Contract -- No --> Infra{Dependency unavailable?}
    Infra -- Yes --> Block[Keep Argo sync blocked; repair dependency]
    Infra -- No --> TestFailure{Generated test failure?}
    TestFailure -- Yes --> BoundedRepair[Run bounded non-weakening repair loop]
    BoundedRepair --> Verified{Quality gate and pytest pass?}
    Verified -- Yes --> Catalog[Persist evidence and versioned test]
    Verified -- No --> Escalate[Retain all attempts and escalate for review]
```

AQE never interprets a retry as permission to mutate shared infrastructure.
Cluster restarts, deployments, and external writes remain explicit platform
operations with their own authorization and audit trail.
