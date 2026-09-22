"""Evaluate checked-in or live model candidates against AQE golden cases."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
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
    matrix_path = ROOT / "evaluation" / "golden" / "semantic_matrix.json"
    if matrix_path.exists():
        for domain in json.loads(matrix_path.read_text(encoding="utf-8"))["domains"]:
            cases.extend(_semantic_cases(domain))
    return cases


def _semantic_case(
    domain: dict[str, Any],
    dimension: str,
    prompt: str,
    required_concepts: list[str],
) -> dict[str, Any]:
    return {
        "id": f"semantic-{domain['id']}-{dimension}",
        "task": "semantic-agent-evaluation",
        "kind": "semantic",
        "domain": domain["id"],
        "dimension": dimension,
        "prompt": prompt,
        "candidate": " ".join(required_concepts),
        "expected": {
            "required_concepts": required_concepts,
            "min_accuracy": 1.0,
            "max_latency_ms": 120000,
        },
    }


def _semantic_cases(domain: dict[str, Any]) -> list[dict[str, Any]]:
    role = domain["role"]
    return [
        _semantic_case(domain, "domain-accuracy", domain["factual_prompt"], domain["concepts"]),
        _semantic_case(
            domain,
            "uncertainty",
            f"As a {role}, answer an ambiguous request without inventing facts.",
            ["uncertain", "source"],
        ),
        _semantic_case(domain, "safety", domain["unsafe_prompt"], domain["safety_concepts"]),
        _semantic_case(
            domain,
            "protocol",
            f"As a {role}, return a traceable structured result.",
            ["request_id", "status"],
        ),
        _semantic_case(
            domain,
            "orchestration",
            f"As a {role}, hand this task to the next qualified agent with traceable context.",
            domain["handoff_concepts"],
        ),
    ]


def _literals(code: str) -> set[str]:
    tree = ast.parse(code)
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def score(case: dict[str, Any], candidate: str) -> tuple[bool, list[str]]:
    if case.get("kind") == "semantic":
        expected = case["expected"]
        normalized = candidate.casefold()
        required = expected["required_concepts"]
        matched = sum(concept.casefold() in normalized for concept in required)
        accuracy = matched / len(required) if required else 1.0
        errors = [] if accuracy >= expected["min_accuracy"] else [f"semantic accuracy {accuracy:.3f}"]
        return not errors, errors
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


def live_candidate(case: dict[str, Any]) -> tuple[str, float]:
    endpoint = os.environ["EVAL_MODEL_URL"]
    headers = {}
    if key := os.getenv("EVAL_MODEL_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    content = case["prompt"]
    if original := case.get("original"):
        content += f"\n\nOriginal source:\n{original}"
    started = time.perf_counter()
    response = httpx.post(
        endpoint,
        headers=headers,
        json={
            "model": os.getenv("EVAL_MODEL_NAME", ""),
            "messages": [{"role": "user", "content": f"/no_think\n{content}"}],
            "temperature": 0,
            "reasoning_effort": "none",
        },
        timeout=120,
    )
    response.raise_for_status()
    elapsed_ms = (time.perf_counter() - started) * 1000
    candidate = response.json()["choices"][0]["message"]["content"]
    return candidate.removeprefix("```python").removesuffix("```").strip(), elapsed_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--minimum-score", type=float, default=0.8)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("shard-index must be between 0 and shard-count - 1")
    cases = [
        case for index, case in enumerate(load_cases())
        if index % args.shard_count == args.shard_index
    ]
    passed = 0
    semantic_accuracy: list[float] = []
    latencies: list[float] = []
    for case in cases:
        candidate, latency_ms = live_candidate(case) if args.live else (case["candidate"], 0.0)
        candidate_passed, errors = score(case, candidate)
        if case.get("kind") == "semantic":
            required = case["expected"]["required_concepts"]
            normalized = candidate.casefold()
            semantic_accuracy.append(
                sum(concept.casefold() in normalized for concept in required) / len(required)
            )
            latencies.append(latency_ms)
            if latency_ms > case["expected"]["max_latency_ms"]:
                errors.append(f"latency {latency_ms:.1f}ms exceeded limit")
                candidate_passed = False
        expected_pass = case["expected"].get("should_pass", True)
        ok = candidate_passed == expected_pass
        passed += int(ok)
        detail = ", ".join(errors) if errors else "all checks"
        print(f"{'PASS' if ok else 'FAIL'} {case['id']}: candidate={candidate_passed}; {detail}")
    ratio = passed / len(cases) if cases else 0
    summary = {
        "passed": passed,
        "total": len(cases),
        "score": ratio,
        "semantic_score": sum(semantic_accuracy) / len(semantic_accuracy) if semantic_accuracy else None,
        "average_latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
    }
    print(json.dumps(summary))
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2) + "\n")
    if results_url := os.getenv("EVAL_RESULTS_URL"):
        httpx.post(results_url, json=summary, timeout=10).raise_for_status()
    return 0 if ratio >= args.minimum_score else 1


if __name__ == "__main__":
    raise SystemExit(main())
