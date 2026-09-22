from agents.github_analysis.app import inspect_python_source


def test_source_analysis_finds_bare_except():
    findings = inspect_python_source("agent.py", "try:\n    run()\nexcept:\n    pass\n")

    assert [finding["rule"] for finding in findings] == ["bare-except"]


def test_source_analysis_finds_request_without_timeout():
    findings = inspect_python_source("agent.py", "import requests\nrequests.get('https://agent')\n")

    assert [finding["rule"] for finding in findings] == ["http-without-timeout"]


def test_source_analysis_accepts_request_with_timeout():
    findings = inspect_python_source("agent.py", "import requests\nrequests.get('https://agent', timeout=5)\n")

    assert findings == []
