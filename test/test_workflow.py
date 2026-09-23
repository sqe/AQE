from temporal_worker.workflow import error_message, partition_scenarios


def test_nested_child_failure_reports_actionable_root_cause():
    root = RuntimeError("Quality Oracle rejected generated tests: invented expected response")
    child = RuntimeError("Child Workflow execution failed")
    child.__cause__ = root

    assert error_message(child) == (
        "Quality Oracle rejected generated tests: invented expected response: "
        "Child Workflow execution failed"
    )


def test_requirements_needed_scenarios_are_not_sent_to_generation():
    executable = {
        "scenario_id": "declared-contract",
        "execution_status": "executable",
        "contract_gaps": ["semantic_oracle"],
    }
    incomplete = {
        "scenario_id": "missing-invocation",
        "execution_status": "requirements_needed",
        "contract_gaps": ["invocation", "prompt", "semantic_oracle"],
    }

    selected, requirements_needed = partition_scenarios([incomplete, executable])

    assert selected == [executable]
    assert requirements_needed == [incomplete]


def test_legacy_scenario_without_execution_status_remains_executable():
    scenario = {"scenario_id": "caller-supplied"}

    selected, requirements_needed = partition_scenarios([scenario])

    assert selected == [scenario]
    assert requirements_needed == []
