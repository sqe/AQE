#!/usr/bin/env bash
set -euo pipefail

candidate_sha="${1:-$(git rev-parse origin/main)}"
namespace="${AQE_NAMESPACE:-aqe}"
release="${AQE_RELEASE:-aqe}"

if [[ ! "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "candidate SHA must be a full 40-character commit" >&2
  exit 2
fi
git merge-base --is-ancestor "$candidate_sha" origin/main || {
  echo "candidate is not reachable from origin/main" >&2
  exit 2
}

tag="sha-${candidate_sha:0:7}"
values_file=$(mktemp)
trap 'rm -f "$values_file"' EXIT

cat >"$values_file" <<EOF
agents:
  agent-builder: {image: ghcr.io/sqe/aqe-agent-builder:$tag}
  artifact-management: {image: ghcr.io/sqe/aqe-artifact-management:$tag}
  change-detection: {image: ghcr.io/sqe/aqe-change-detection:$tag}
  dbt-builder: {image: ghcr.io/sqe/aqe-dbt-builder:$tag}
  diagnostics: {image: ghcr.io/sqe/aqe-diagnostics:$tag}
  github-analysis: {image: ghcr.io/sqe/aqe-github-analysis:$tag}
  github-connector: {image: ghcr.io/sqe/aqe-github-connector:$tag}
  github-commit: {image: ghcr.io/sqe/aqe-github-commit:$tag}
  knowledge-ingestion: {image: ghcr.io/sqe/aqe-knowledge-ingestion:$tag}
  quality-oracle: {image: ghcr.io/sqe/aqe-quality-oracle:$tag}
  test-generation: {image: ghcr.io/sqe/aqe-test-generation:$tag}
  test-execution: {image: ghcr.io/sqe/aqe-test-execution:$tag}
  webpage-state-capture: {image: ghcr.io/sqe/aqe-webpage-state-capture:$tag}
  website-execution: {image: ghcr.io/sqe/aqe-website-execution:$tag}
images:
  temporal: ghcr.io/sqe/aqe-temporal:$tag
  byoa: ghcr.io/sqe/aqe-byoa:$tag
  reporting: ghcr.io/sqe/aqe-reporting:$tag
  frontend: ghcr.io/sqe/aqe-frontend:$tag
config:
  githubSourceEvaluationRef: $candidate_sha
EOF

helm upgrade --install "$release" deploy/helm/aqe \
  --namespace "$namespace" --create-namespace \
  -f deploy/helm/aqe/values.yaml \
  -f deploy/helm/aqe/values-agentic-platform.yaml \
  -f "$values_file" \
  --force-conflicts --rollback-on-failure --wait --timeout 15m

kubectl -n "$namespace" get deployment \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[*].image'
echo "Candidate $candidate_sha ($tag) deployed to $namespace."
