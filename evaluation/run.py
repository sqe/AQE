"""Evaluate checked-in or live model candidates against AQE golden cases."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.test_execution.test_quality import inspect_test_code


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted((ROOT / "evaluation" / "golden").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append(json.loads(line))
    return cases


def _literals(code: str) -> set[str]:
    tree = ast.parse(code)
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def score(case: dict[str, Any], candidate: str) -> tuple[bool, list[str]]:
    errors: list[str] = []
    expected = case["expected"]
    issues = inspect_test_code(candidate)
    if expected.get("valid_python", True) and any(issue.rule == "valid-python" for issue in issues):
        errors.append("invalid Python")
    if any(issue.rule in {"single-outcome", "observable-outcome"} for issue in issues):
        errors.append("tests are not atomic")
    for forbidden in expected.get("forbidden", []):
        if forbidden in candidate:
            errors.append(f"contains forbidden pattern: {forbidden}")
    literals = _literals(candidate)
    for literal in expected.get("preserve_literals", []):
        if literal not in literals:
            errors.append(f"did not preserve expected literal: {literal}")
    test_count = sum(
        1
        for node in ast.walk(ast.parse(candidate))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    )
    if "test_count" in expected and test_count != expected["test_count"]:
        errors.append(f"expected {expected['test_count']} tests, got {test_count}")
    return not errors, errors


def live_candidate(case: dict[str, Any]) -> str:
    endpoint = os.environ["EVAL_MODEL_URL"]
    headers = {}
    if key := os.getenv("EVAL_MODEL_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    content = case["prompt"]
    if original := case.get("original"):
        content += f"\n\nOriginal source:\n{original}"
    response = httpx.post(
        endpoint,
        headers=headers,
        json={
            "model": os.getenv("EVAL_MODEL_NAME", ""),
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].removeprefix("```python").removesuffix("```").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--minimum-score", type=float, default=0.8)
    args = parser.parse_args()
    cases = load_cases()
    passed = 0
    for case in cases:
        candidate = live_candidate(case) if args.live else case["candidate"]
        candidate_passed, errors = score(case, candidate)
        expected_pass = case["expected"].get("should_pass", True)
        ok = candidate_passed == expected_pass
        passed += int(ok)
        detail = ", ".join(errors) if errors else "all checks"
        print(f"{'PASS' if ok else 'FAIL'} {case['id']}: candidate={candidate_passed}; {detail}")
    ratio = passed / len(cases) if cases else 0
    print(json.dumps({"passed": passed, "total": len(cases), "score": ratio}))
    return 0 if ratio >= args.minimum_score else 1


if __name__ == "__main__":
    raise SystemExit(main())
