import json

from evaluation.run import load_cases, score


def test_golden_dataset_has_unique_ids():
    cases = load_cases()

    assert len({case["id"] for case in cases}) == len(cases)


def test_golden_dataset_cases_match_expected_verdicts():
    mismatches = []
    for case in load_cases():
        candidate_passed, errors = score(case, case["candidate"])
        expected_pass = case["expected"].get("should_pass", True)
        if candidate_passed != expected_pass:
            mismatches.append({"id": case["id"], "errors": errors})

    assert json.dumps(mismatches, sort_keys=True) == "[]"


def test_semantic_case_fails_when_required_concept_is_missing():
    case = next(item for item in load_cases() if item.get("kind") == "semantic")

    passed, _ = score(case, "A fluent but ungrounded response.")

    assert passed is False


def test_semantic_matrix_expands_to_one_hundred_cases():
    semantic_cases = [case for case in load_cases() if case.get("kind") == "semantic"]

    assert len(semantic_cases) == 100
