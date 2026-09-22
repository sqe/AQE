import asyncio

from byoa_adapter.app import _normalize, handle


def test_model_fleet_envelope_is_normalized():
    payload = {
        "jsonrpc": "2.0",
        "id": "request-1",
        "method": "tasks.execute",
        "params": {"skill": "qe.repair", "context": {"task_id": "run-1"}},
    }

    normalized = _normalize(payload)

    assert normalized == ("request-1", "qe.repair", {"task_id": "run-1"})


def test_unknown_skill_returns_json_rpc_error():
    result = asyncio.run(
        handle({"jsonrpc": "2.0", "id": "request-2", "method": "qe.unknown", "params": {}})
    )

    assert result["error"]["code"] == -32601
