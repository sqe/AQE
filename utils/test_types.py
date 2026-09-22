"""Shared deterministic selection of generated-test execution runtimes."""

from typing import Any


def resolve_test_type(captured_state: dict[str, Any]) -> str:
    requested = str(captured_state.get("test_type", "")).lower().strip()
    if requested in {"agent", "website"}:
        return requested
    if captured_state.get("agent_card_url") or captured_state.get("target_agent"):
        return "agent"
    return "website"
