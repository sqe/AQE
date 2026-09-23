# AQE: verified testing for AI-native systems

## The 30-second pitch

AQE turns an agent's declared behavior, product requirements, and optional
source evidence into executable tests. It reviews generated tests before they
run, executes them in bounded environments, retains complete evidence, and
promotes only validated tests into a versioned regression catalog.

The goal is not to make an LLM the final judge of quality. The goal is to make
AI useful inside a deterministic verification system:

```text
requirements + contracts → generate → static gate → independent review
                         → sandbox execution → evidence → regression catalog
```

This gives teams faster coverage without turning model output into an automatic
production approval.

## What AQE does

- Discovers Agent Cards, skills, invocation contracts, versions, and declared
  semantic outcomes across an agent fleet.
- Generates atomic HTTP, agent-protocol, browser, integration, performance, and
  security tests from supplied evidence.
- Uses product knowledge, a governed agent ontology, and optionally pinned,
  allowlisted source code to ground generation.
- Applies deterministic source checks and an independent Quality Oracle before
  executing generated code.
- Runs tests in bounded HTTP or Playwright runtimes, diagnoses failures, and
  permits only limited repairs that preserve expected behavior.
- Stores run state and provenance in PostgreSQL and large immutable artifacts in
  RustFS.
- Publishes only passing, quality-gated suites to the `aqe-generated-tests`
  branch. Explicitly confirmed product defects can enter a separate findings
  catalog.
- Continuously reruns cataloged tests against their declared target and version.
- Connects PR verification, immutable candidate images, Autopilot evidence,
  generated-test CI, approved release tags, and GitOps production promotion.

## What AQE does not do

- It does not invent missing endpoints, payloads, requirements, or expected
  answers. Missing contracts remain visible as `requirements_needed`.
- It does not treat Temporal `COMPLETED`, an LLM response, or a generated file as
  proof that a test passed.
- It does not publish unreviewed model output, ordinary failures, environment
  errors, or suspected defects into the regression catalog.
- It does not silently weaken assertions during repair.
- It does not let generated builders deploy code, mutate Kubernetes, or connect
  generated dbt projects to a warehouse. Their output is review-only.
- It does not restart unhealthy workloads through “safe heal”; that operation
  returns diagnostics and recommendations only.
- It does not replace security review, release approval, production monitoring,
  or authoritative product requirements.

## Why Temporal

End-to-end quality workflows are long-running and failure-prone. Generation,
independent review, sandbox execution, repair, and fleet campaigns can each take
minutes and can cross process or pod restarts. A normal request handler is not a
reliable owner for that lifecycle.

Temporal is AQE's durable control plane. It owns workflow identity, ordering,
timeouts, bounded retries, cancellation, child workflows, and recoverable state.
Activities call the specialist services; Temporal records what should happen
next. PostgreSQL and RustFS retain testing evidence, but they do not replace the
workflow state machine.

Temporal is deliberately not used as the platform event bus. It coordinates one
run after AQE accepts it.

## Why Kafka

Kafka is the asynchronous boundary between the larger agent platform and AQE's
BYOA adapter. The platform submits JSON-RPC tasks to `tasks.aqe`; AQE publishes
correlated responses to `results.aqe`. This decouples producers from AQE's HTTP
availability and supports independent scaling and replay.

The consumer disables automatic offset commits. It publishes the result first,
using an idempotent producer with `acks=all`, and commits the consumed offset
only afterward. This is **at-least-once delivery**, not a claim of end-to-end
exactly-once execution; request IDs and downstream idempotency remain important.

Kafka does not coordinate generation or retries inside a run. The adapter
normalizes and validates the platform message, then starts or queries the
Temporal-owned workflow. Keeping these roles separate avoids rebuilding a
workflow engine on top of Kafka consumers.

## Testing guardrails

AQE layers deterministic and model-based checks so no single mechanism must be
trusted alone:

1. **Evidence boundary:** generation may use only supplied requirements,
   executable contract scenarios, governed ontology rules, retrieved product
   evidence, and pinned allowlisted source evidence.
2. **Static quality gate:** Python must parse; tests must be atomic, portable,
   meaningfully named, and mapped to every declared skill dimension. Multiple
   unrelated assertions, fixed sleeps, missing outcomes, and invalid mappings
   are rejected before Oracle tokens or sandbox time are spent.
3. **Independent review:** the Quality Oracle compares the immutable candidate
   with the original evidence. Ambiguous, malformed, unavailable, or rejected
   review fails closed. High-impact workloads can require a model independent
   from the generator.
4. **Bounded execution:** generated code runs in the correct isolated HTTP or
   browser runtime with time, resource, authentication, and network controls.
5. **Repair integrity:** retries are limited and cannot remove coverage, change
   expected values, or convert uncertainty into a pass.
6. **Promotion gate:** only successful pytest/JUnit outcomes that passed every
   prior gate can enter the green catalog. Failures remain evidence until
   explicitly classified.

## Golden datasets and model verification

The checked-in golden evaluation suite contains 105 cases:

- 100 semantic cases: 20 agent domains × domain accuracy, uncertainty, safety,
  protocol behavior, and orchestration.
- 5 generation and repair cases: valid Python, atomic outcomes, stable waiting,
  forbidden weak patterns, and preservation of expected literals.

Ordinary CI validates the corpus, scorer, and deterministic expectations on
every relevant code change. Scheduled and SemVer release workflows can call the
serving model and run the cases in ten shards, recording score, semantic score,
latency, and case count. Offline validation proves the evaluation machinery;
only live evaluation measures the model that will serve users.

Golden evaluation is a regression signal, not a universal truth set. New escaped
defects and changed product risks should become reviewed cases so the gate grows
with the system.

## How verification and release are automated

1. **Pull request:** changed paths select docs, config, or code verification and
   only the affected image builds. Runtime-owned changes add disposable E2E.
2. **Merge queue:** the same gates run against the exact combined commit intended
   for `main`.
3. **Immutable build:** `main` publishes every multi-architecture image as
   `sha-<commit>` with provenance and SBOM attestations.
4. **Candidate qualification:** that exact SHA is deployed. Discovery must show
   complete executable contracts and semantic oracles; Autopilot must return
   business success for every child.
5. **Continuous test product:** each passing child returns a generated-catalog
   commit. The separate catalog workflow reruns those tests against configured
   targets.
6. **Approved promotion:** the release workflow verifies candidate images,
   model connectivity, fleet evidence, catalog ancestry, and catalog CI before
   creating `vX.Y.Z`.
7. **Build once, promote many:** the tag aliases the already-verified SHA images
   rather than rebuilding them. Live model evaluation and disposable integration
   checks must pass before the GitHub Release is published.
8. **Production GitOps:** a reviewed digest update lets Argo CD reconcile the
   same verified artifacts. Rollback restores the previous digests in Git.

The result is a traceable chain from a source change to tests, immutable images,
runtime evidence, catalog commits, release approval, and production deployment.

## The value

AQE increases verification throughput without lowering the release bar. Teams
get generated coverage and fleet-wide automation, while deterministic gates,
durable orchestration, independent review, sandboxing, and immutable evidence
keep the final decision auditable and reproducible.

For implementation detail, see [Architecture and operating rules](architecture.md),
[Testing agents](testing-agents.md), and [Production release](production-release.md).
