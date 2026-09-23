"""Static quality gates shared by isolated generated-test runtimes.

These checks reject structurally poor generated tests before untrusted code is
executed. They intentionally do not rewrite test intent.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


REQUIRED_SKILL_DIMENSIONS = {"positive", "protocol_schema", "malformed_input", "latency"}
TEST_LAYERS = {"api", "db", "ui", "agentic", "integration", "performance", "security"}


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


def _literal_assignment(tree: ast.AST, name: str) -> Any:
    for node in getattr(tree, "body", []):
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and ((isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))
                 or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name))
        ):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
    return None


def inspect_test_code(
    code: str,
    required_skills: list[str] | None = None,
    required_dimensions_by_skill: dict[str, list[str]] | None = None,
    require_semantic_names: bool = False,
) -> list[QualityIssue]:
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
    test_names = {test.name for test in tests}
    required_skills = [str(skill) for skill in (required_skills or []) if skill]
    if required_skills or require_semantic_names:
        test_layer = _literal_assignment(tree, "AQE_TEST_LAYER")
        if test_layer not in TEST_LAYERS:
            issues.append(
                QualityIssue(
                    "test-layer",
                    f"AQE_TEST_LAYER must be one of: {', '.join(sorted(TEST_LAYERS))}",
                )
            )
        suite_id = _literal_assignment(tree, "AQE_SUITE_ID")
        if (
            not isinstance(suite_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", suite_id)
            or suite_id in {"generated-test", "agent-test", "test-suite"}
        ):
            issues.append(
                QualityIssue(
                    "semantic-suite-name",
                    "AQE_SUITE_ID must be a semantic kebab-case capability name such as dbt-project-validation",
                )
            )
    if required_skills:
        skill_tests = _literal_assignment(tree, "AQE_SKILL_TESTS")
        if not isinstance(skill_tests, dict):
            issues.append(
                QualityIssue(
                    "skill-coverage-plan",
                    "AQE_SKILL_TESTS must map every advertised skill to executable test dimensions",
                )
            )
        else:
            for skill in required_skills:
                dimensions = skill_tests.get(skill)
                if not isinstance(dimensions, dict):
                    issues.append(QualityIssue("advertised-skill-coverage", f"No executable tests mapped for {skill}"))
                    continue
                required_dimensions = set(
                    (required_dimensions_by_skill or {}).get(skill) or REQUIRED_SKILL_DIMENSIONS
                )
                missing = required_dimensions - set(dimensions)
                if missing:
                    issues.append(
                        QualityIssue(
                            "advertised-skill-coverage",
                            f"{skill} is missing test dimensions: {', '.join(sorted(missing))}",
                        )
                    )
                invalid_mappings = {
                    str(dimension)
                    for dimension, references in dimensions.items()
                    if not (
                        isinstance(references, str)
                        or (
                            isinstance(references, list)
                            and bool(references)
                            and all(isinstance(reference, str) for reference in references)
                        )
                    )
                }
                referenced_tests = [
                    (str(dimension), reference)
                    for dimension, references in dimensions.items()
                    for reference in (references if isinstance(references, list) else [references])
                    if isinstance(reference, str)
                ]
                unknown_tests = {
                    test_name
                    for _dimension, test_name in referenced_tests
                    if test_name not in test_names
                }
                if invalid_mappings or unknown_tests:
                    invalid_references = sorted(invalid_mappings | unknown_tests)
                    issues.append(
                        QualityIssue(
                            "skill-test-reference",
                            f"{skill} has invalid dimensions or missing pytest functions: {', '.join(invalid_references)}",
                        )
                    )
                skill_name = re.sub(r"[^a-z0-9]+", "_", skill.lower()).strip("_")
                non_semantic = {
                    test_name
                    for dimension, test_name in referenced_tests
                    if skill_name not in test_name.lower() or dimension.lower() not in test_name.lower()
                }
                if non_semantic:
                    issues.append(
                        QualityIssue(
                            "semantic-test-name",
                            f"{skill} test names must include the skill and dimension: {', '.join(sorted(non_semantic))}",
                        )
                    )
    endpoint_environment = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "getenv"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in {"AGENT_BASE_URL", "AGENT_CARD_URL", "TARGET_BASE_URL"}
    }
    internal_urls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("http://", "https://"))
        and (
            (urlsplit(node.value).hostname or "").endswith(".svc")
            or (urlsplit(node.value).hostname or "").startswith("aqe-")
        )
    ]
    if internal_urls and not endpoint_environment:
        issues.append(
            QualityIssue(
                "portable-endpoint",
                "Cluster-local URLs require an AGENT_BASE_URL, AGENT_CARD_URL, or TARGET_BASE_URL environment override",
                internal_urls[0].lineno,
            )
        )
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
        for assertion in assertions:
            if isinstance(assertion.test, ast.Constant) and assertion.test.value is True:
                issues.append(
                    QualityIssue(
                        "non-trivial-outcome",
                        f"{test.name} uses an unconditional passing assertion",
                        assertion.lineno,
                    )
                )
    return issues
