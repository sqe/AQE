# Testing current and new agents

AQE tests an agent in three stages: validate its Agent Card, generate atomic
pytest scenarios from declared behavior, then execute those tests in the
browser-free agent sandbox. Semantic accuracy is measured only when an expected
response comes from an Agent Card evaluation case or an uploaded requirements
document; AQE does not invent a domain oracle.

## 1. Verify the platform

Start the local model and establish API forwards:

```bash
kubectl get --raw='/readyz'
kubectl -n aqe port-forward svc/aqe-diagnostics 18006:8006
kubectl -n aqe port-forward svc/aqe-workflow-api 18008:8008
```

Keep the two port-forward commands running in separate terminals. Verify the
fleet and model before creating a run:

```bash
curl -s http://127.0.0.1:18006/v1/diagnostics | jq '{status,healthy,total}'

kubectl -n aqe exec deployment/aqe-test-generation -- python -c \
  "import os,httpx; r=httpx.post(os.environ['LLM_GENERATION_ENDPOINT'],json={'model':os.environ['LLM_GENERATION_MODEL'],'messages':[{'role':'user','content':'/no_think\\nReply exactly AQE_READY'}],'max_tokens':20,'reasoning_effort':'none'},timeout=90); print(r.status_code,r.json()['choices'][0]['message']['content'])"
```

Expected results are `status=healthy`, all configured agents healthy, and
`200 AQE_READY`.

## 2. Dogfood AQE against its own agent fleet

In the UI, open **01 / DISCOVER**, leave Agent Card URL
blank, and click **DISCOVER ALL CONFIGURED AGENTS**. Supplying a URL limits the
operation to that agent and its bounded orchestration links. The equivalent API
request is:

```bash
curl -s -X POST http://127.0.0.1:18006/v1/agent-discovery \
  -H 'content-type: application/json' \
  -d '{"max_depth":1,"max_latency_ms":5000,"min_accuracy":0.8}' \
  | jq '{summary,agents:[.agents[]|{identity,status,skills,scenarios}]}'
```

Discovery checks identity, version, declared skills, status, executable
invocation contracts, and bounded orchestration peers. Interpret the two gaps
separately:

- `execution_status=requirements_needed` means AQE cannot call the skill because
  its invocation URL or example payload is absent. It is not counted as passed.
- `oracle_status=requirements_needed` means protocol, schema, malformed-input,
  and latency tests can run, but semantic accuracy cannot be scored until a
  domain-reviewed expected outcome is supplied.

**AUTOPILOT MODE** additionally searches every allowlisted repository through
the GitHub MCP connector for `agent.yaml`, correlates those manifests with live
Agent Cards, and source-grounds matched runs at the discovered commit. It starts
one durable fleet workflow whose child runs are bounded to protect Qwen and
serialize catalog writes. Repository-only agents are listed as
`endpoint_required` until their manifest declares a reachable `spec.cardUrl` or
their runtime Agent Card is configured.

Click **RUN AUTOPILOT ON ENTIRE FLEET** once, then open **04 / OBSERVE**. This is
AQE's dogfood campaign: every configured AQE Agent Card is correlated with its
manifest and pinned source tree, generated suites pass through the independent
Quality Oracle, approved suites execute in the agent sandbox, and validated
results are cataloged in GitHub. On the first rejection, Temporal regenerates
the complete suite once with the Oracle's structured issues and coverage gaps,
then fails closed if the repaired candidate is still rejected. The 2D and 3D
views consume the same lifecycle events; Temporal remains the authoritative
durable history.

Generation is change-aware. AQE fingerprints the target identity/version,
advertised skills and scenarios, pinned source tree, requirements, ontology
classification, thresholds, and generation-policy version. An unchanged input
reuses the latest passed, cataloged suite and executes it again; it does not
spend model capacity creating another test. A changed source tree, contract,
requirements set, threshold, or policy produces a new candidate suite.
When a supported Agent Card supplies a complete machine-readable contract, AQE
compiles its suite deterministically instead of spending generation-model
tokens. The compiled suite still passes through the Oracle and execution gate.

## 3. Generate and execute a test for a current agent

This example tests the diagnostics agent. Keep each expected behavior explicit
and independently observable.

```bash
run=$(curl -s -X POST http://127.0.0.1:18008/v1/qe-runs \
  -H 'content-type: application/json' \
  -d '{
    "url":"http://aqe-diagnostics:8006",
    "agent_card_url":"http://aqe-diagnostics:8006/agent_card",
    "test_type":"agent",
    "agent_name":"aqe-diagnostics",
    "agent_version":"2.0.0",
    "spec":"Create atomic pytest HTTP tests that invoke diagnostics.scan and verify its positive result, response schema, malformed-input behavior, and latency. Do not substitute Agent Card membership for skill execution.",
    "repair":false,
    "max_latency_ms":5000,
    "min_accuracy":0.8
  }')

echo "$run" | jq
workflow_id=$(echo "$run" | jq -r .workflow_id)
watch -n 3 "curl -s http://127.0.0.1:18008/v1/qe-runs/$workflow_id | jq"
```

`COMPLETED` means Temporal completed orchestration. Confirm
`result.successful == true` and inspect `result.summary` to prove pytest passed;
a completed workflow may correctly report a product assertion failure or an
execution error. A passing test is published to the configured
`TEST_CATALOG_REPOSITORY` on `TEST_CATALOG_BRANCH`.

Catalog CI must be able to reach the tested version. Set the
`generated-agent-e2e` environment variables `AGENT_BASE_URL`,
`AGENT_CARD_URL`, or `TARGET_BASE_URL` to a routable endpoint. For private or
local agents, set repository variable `E2E_RUNNER` to the label of a secured
self-hosted runner on that network; do not use an unreachable Kubernetes
Service hostname on a GitHub-hosted runner.

To ground generation in source, add an allowlisted repository and immutable
ref to the request:

```json
{
  "source_repository": "sqe/AQE",
  "source_ref": "<full-commit-sha>"
}
```

## 4. Prepare a new agent

Publish an Agent Card at `/.well-known/agent.json` or `/agent_card`. Every skill
needs an executable `invocation`; include evaluation cases when semantic
accuracy should be measured:

```json
{
  "name": "lesson-agent",
  "version": "1.4.0",
  "url": "https://lesson-agent.example/rpc",
  "status": "UP",
  "ontology": {"archetype": "teaching_coaching"},
  "skills": [
    {
      "id": "lesson.explain",
      "description": "Explain a concept using the supplied lesson",
      "examples": ["Explain why seasons occur"],
      "invocation": {
        "protocol": "a2a_jsonrpc",
        "method": "tasks.execute",
        "url": "https://lesson-agent.example/rpc"
      }
    }
  ],
  "evaluation": {
    "cases": [
      {
        "skill_id": "lesson.explain",
        "prompt": "Explain why seasons occur",
        "expected_response": "Seasons are caused by Earth's axial tilt, not its distance from the Sun.",
        "max_latency_ms": 5000,
        "min_accuracy": 0.85
      }
    ]
  }
}
```

The expected response must describe the required meaning rather than exact
styling. Use domain-reviewed golden answers for teaching, finance, insurance,
healthcare, legal, or other high-impact professions.

AQE classifies cards that omit `ontology.archetype` from their name,
description, skill IDs/descriptions, and tags. Explicit valid archetypes always
win. The diagnostics scan also builds a dynamic fleet-family overlay: canonical
families are immediately routable, while a skill namespace recurring across at
least two unclassified agents becomes a review-required candidate family. AQE
never silently promotes an observed pattern into the governed ontology.

AQE emits `AQE_SKILL_TESTS` in every generated agent test. The execution gate
rejects a catalog candidate unless every advertised skill maps to real atomic
tests for `positive`, `protocol_schema`, `malformed_input`, and `latency`.
Scenarios with expected responses additionally require `semantic_accuracy`.
Each dimension can map to one test or a non-empty list of tests, allowing every
declared schema and semantic invariant to remain independently executable.
Cards may add dimensions such as `authentication`, `idempotency`,
`cancellation`, or `orchestration` through `required_dimensions`.
Every generated file also declares a primary `AQE_TEST_LAYER` and semantic
`AQE_SUITE_ID`. GitHub stores validated suites under
`generated-tests/<runtime>/<layer>/<agent>/<release-version>/<git-revision>/`;
the task trace is only a short suffix, while suite and pytest function names
describe the capability and expected behavior.
Catalog filenames repeat the semantic target, suite, and release version before
the short trace—for example
`test_claims_agent__claim_eligibility__v2_1__task_42.py`—so files remain
distinguishable in GitHub search results and downloaded artifacts.

This provides structural coverage of 100% of **advertised executable skills**;
it is not a claim that arbitrary hidden behavior is 100% correct. A skill
without an invocation or a business assertion without a reviewed expected
response is reported as a coverage gap. Once those contracts exist, AQE checks
business behavior, protocol/schema, malformed input, latency, and declared
semantic accuracy rather than merely checking that a skill name appears in the
Agent Card.

## dbt builder end-to-end check

The dbt builder publishes executable contracts for both
`dbt.blueprint.build` and `dbt.project.validate`. Confirm connectivity and its
deterministic business rules before spending model capacity:

```bash
kubectl -n aqe port-forward svc/aqe-dbt-builder 18016:8016
curl -fsS http://127.0.0.1:18016/agent_card | jq '.skills, .evaluation.cases'

curl -fsS -X POST http://127.0.0.1:18016/v1/projects/validate \
  -H 'content-type: application/json' \
  --data @examples/dbt-validation-request.json | jq

curl -fsS http://127.0.0.1:18016/metrics | \
  grep -E 'aqe_(dbt|http_request)'
```

Run the opt-in live E2E suite against that forward:

```bash
DBT_BUILDER_E2E_URL=http://127.0.0.1:18016 \
  python -m pytest -q test/e2e/test_dbt_builder_live.py

# Also call Qwen and validate the packaged review-only blueprint:
DBT_BUILDER_E2E_URL=http://127.0.0.1:18016 RUN_DBT_MODEL_E2E=true \
  python -m pytest -q test/e2e/test_dbt_builder_live.py
```

To verify both runner metric surfaces, forward their services and run:

```bash
AQE_AGENT_RUNNER_E2E_URL=http://127.0.0.1:18003 \
AQE_WEBSITE_RUNNER_E2E_URL=http://127.0.0.1:18013 \
  python -m pytest -q test/e2e/test_runner_metrics_live.py
```

## Temporal workflow history and UI

AQE's Graph tab reads bounded workflow summaries from Temporal and links each
run to Temporal's authoritative event history. Temporal UI is an optional
component and may not be installed with the Temporal Frontend service. Check
before forwarding it:

```bash
kubectl -n temporal get svc
```

The Agentic Platform Temporal service exposes its built-in UI on port 8233:

```bash
kubectl -n temporal port-forward --address 127.0.0.1 \
  svc/temporal-frontend 18088:8233
```

Then open `http://127.0.0.1:18088`. The Helm value
`config.temporalUiUrl` controls the link shown in AQE. Pin and review the image
version in the owning platform repository for durable environments. A running
workflow may briefly show a zero visibility-history count in list APIs; use its
Temporal history link for the authoritative event stream.

AUTOPILOT then generates the live model-backed build suite from the card's
versioned order-project case. A valid build must remain review-only, produce all
staging/intermediate/mart layers, include documentation and data tests, satisfy
the declared latency budget, and expose mission outcome/duration metrics. The
Grafana overview separately charts agent HTTP p95, dbt mission p95/outcomes,
and agent/website runner duration/outcomes.

## 5. Connect a new internal or external agent

Add only its exact hostname to Helm `config.agentProbeAllowedHosts`. An agent in
the `aqe` namespace is reachable through the chart's same-namespace policy. An
agent in another namespace needs a narrow NetworkPolicy egress rule for its
namespace and port. External agents must use HTTPS port 443 unless a reviewed,
specific CIDR/port rule is added.

Probe before generating tests:

```bash
curl -s -X POST http://127.0.0.1:18006/v1/agent-discovery \
  -H 'content-type: application/json' \
  -d '{
    "card_urls":["https://lesson-agent.example/.well-known/agent.json"],
    "max_depth":2,
    "max_agents":20,
    "max_latency_ms":5000,
    "min_accuracy":0.85
  }' | jq
```

Then use the request from section 3 with the new base URL, card URL, stable
agent name, pinned version, and domain-reviewed observable behavior. Configure
only `TARGET_AUTH_*` secrets needed by that target; AQE platform, model, object
store, and GitHub credentials are never forwarded to generated tests.

## 6. Website testing is separate

For a browser journey use `"test_type":"website"` and a website URL. Omit the
Agent Card and describe visible user behavior, selectors, authentication, and
expected state. AQE routes these tests to the Playwright image; agent/API tests
remain in the smaller HTTP runtime.

## 7. Interpret live activity

The graph reports `queued → running → passed|failed` for each generated test.
Select a test node to inspect its pytest summary and connections. The activity
rail shows generation and execution events in newest-first order; it is an
operational view, while PostgreSQL and RustFS remain the durable audit record.
