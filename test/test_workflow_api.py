import pytest

from temporal_worker.api import failure_message, normalize_source_repository


def test_repository_slug_is_unchanged():
    assert normalize_source_repository("sqe/AQE") == "sqe/AQE"


def test_github_repository_url_is_normalized():
    assert normalize_source_repository("https://github.com/sqe/AQE.git") == "sqe/AQE"


def test_non_github_repository_shape_is_rejected():
    with pytest.raises(ValueError, match="owner/name"):
        normalize_source_repository("https://example.com/sqe/AQE")


def test_nested_failure_reports_root_cause_first():
    root = RuntimeError("repository must be owner/name")
    wrapper = RuntimeError("activity failed")
    wrapper.__cause__ = root

    assert failure_message(wrapper) == "repository must be owner/name: activity failed"
