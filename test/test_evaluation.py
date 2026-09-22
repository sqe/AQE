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
