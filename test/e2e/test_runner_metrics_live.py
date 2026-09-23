"""Opt-in metrics checks for both generated-test execution runtimes."""

from __future__ import annotations

import os

import httpx
import pytest


@pytest.mark.parametrize(
    ("runner", "url"),
    [
        ("agent", os.getenv("AQE_AGENT_RUNNER_E2E_URL", "")),
        ("website", os.getenv("AQE_WEBSITE_RUNNER_E2E_URL", "")),
    ],
)
def test_runner_exports_outcome_and_duration_metrics(runner: str, url: str):
    if not url:
        pytest.skip(f"AQE_{runner.upper()}_RUNNER_E2E_URL is not configured")

    metrics = httpx.get(f"{url.rstrip('/')}/metrics", timeout=10).text

    assert all(name in metrics for name in ("aqe_test_runs_total", "aqe_test_run_duration_seconds"))
