from observability.metrics import normalized_path


def test_metrics_path_normalizes_uuid():
    assert normalized_path("/v1/runs/123e4567-e89b-12d3-a456-426614174000") == "/v1/runs/:id"


def test_metrics_path_preserves_named_route():
    assert normalized_path("/v1/diagnostics") == "/v1/diagnostics"
