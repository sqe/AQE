from temporal_worker.workflow import error_message


def test_nested_child_failure_reports_actionable_root_cause():
    root = RuntimeError("Quality Oracle rejected generated tests: invented expected response")
    child = RuntimeError("Child Workflow execution failed")
    child.__cause__ = root

    assert error_message(child) == (
        "Quality Oracle rejected generated tests: invented expected response: "
        "Child Workflow execution failed"
    )
