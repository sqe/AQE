"""Static quality gates shared by isolated generated-test runtimes.

These checks reject structurally poor generated tests before untrusted code is
executed. They intentionally do not rewrite test intent.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityIssue:
    rule: str
    message: str
    line: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {"rule": self.rule, "message": self.message, "line": self.line}


def _is_expect_call(node: ast.Call) -> bool:
    function = node.func
    return isinstance(function, ast.Name) and function.id == "expect"


def inspect_test_code(code: str) -> list[QualityIssue]:
    """Return deterministic quality violations without changing the code."""
    if not code.strip():
        return [QualityIssue("non-empty", "Test code must not be empty")]
    if code.lstrip().startswith("```"):
        return [QualityIssue("python-only", "Return Python source without Markdown fences")]

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [QualityIssue("valid-python", exc.msg, exc.lineno)]

    tests = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    if not tests:
        return [QualityIssue("pytest-test", "At least one test_ function is required")]

    issues: list[QualityIssue] = []
    for test in tests:
        assertions = [node for node in ast.walk(test) if isinstance(node, ast.Assert)]
        expectations = [
            node
            for node in ast.walk(test)
            if isinstance(node, ast.Call) and _is_expect_call(node)
        ]
        assertion_count = len(assertions) + len(expectations)
        if assertion_count == 0:
            issues.append(
                QualityIssue(
                    "observable-outcome",
                    f"{test.name} must verify one observable outcome",
                    test.lineno,
                )
            )
        elif assertion_count > 1:
            issues.append(
                QualityIssue(
                    "single-outcome",
                    f"{test.name} has {assertion_count} assertions; split it into atomic tests",
                    test.lineno,
                )
            )
    return issues
