#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -f docker-compose.yml -f test/e2e/compose.yml)

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT

"${compose[@]}" up -d --build postgres rustfs rustfs_init zookeeper kafka test_execution_agent

for _ in $(seq 1 60); do
  if curl --fail --silent http://localhost:8003/health >/dev/null; then
    break
  fi
  sleep 2
done
curl --fail --silent http://localhost:8003/health >/dev/null

task_id="e2e-atomic-test"
printf 'AQE_TEST_LAYER = "integration"\nAQE_SUITE_ID = "executor-persistence-smoke"\n\ndef test_truth():\n    assert 6 * 7 == 42\n' | "${compose[@]}" run --rm -T --entrypoint aws rustfs_init \
  --endpoint-url http://rustfs:9000 s3 cp - s3://agentic-qe-artifacts/artifacts/e2e/test_code.py
"${compose[@]}" exec -T postgres psql -U user -d qe_db -v ON_ERROR_STOP=1 <<SQL
INSERT INTO test_runs (task_id, app_id, status, object_path)
VALUES ('$task_id', 'ci', 'PENDING', 'artifacts/e2e/test_code.py')
ON CONFLICT (task_id) DO UPDATE SET status = 'PENDING';
SQL

result="$(curl --silent --show-error http://localhost:8003/run_tests \
  -H 'content-type: application/json' \
  -d "{\"task_id\":\"$task_id\",\"repair\":false}")"
python3 -c 'import json,sys; result=json.load(sys.stdin); assert result["successful"] is True, result' <<<"$result"
"${compose[@]}" exec -T postgres psql -U user -d qe_db -tAc \
  "SELECT status FROM test_runs WHERE task_id = '$task_id'" | grep -qx PASSED

metrics="$(curl --fail --silent http://localhost:8003/metrics)"
grep -q '^aqe_test_runs_total' <<<"$metrics"
grep -q '^aqe_test_run_duration_seconds' <<<"$metrics"
