import httpx

from utils.target_auth import TargetAuth, authenticated_client, browser_context_options


def test_bearer_auth_sets_authorization_header():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    client = authenticated_client(TargetAuth(kind="bearer", token="target-token"), transport=transport)

    request = client.build_request("GET", "https://agent.example/health")

    assert request.headers["authorization"] == "Bearer target-token"


def test_api_key_auth_uses_configured_header():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    client = authenticated_client(
        TargetAuth(kind="api_key", api_key="target-key", api_key_header="X-Agent-Key"),
        transport=transport,
    )

    request = client.build_request("GET", "https://agent.example/health")

    assert request.headers["x-agent-key"] == "target-key"


def test_basic_auth_is_available_to_browser_context():
    options = browser_context_options(TargetAuth(kind="basic", username="learner", password="secret"))

    assert options == {"http_credentials": {"username": "learner", "password": "secret"}}


def test_unknown_auth_type_is_rejected():
    try:
        authenticated_client(TargetAuth(kind="magic"))
    except ValueError as error:
        message = str(error)
    else:
        message = ""

    assert message == "Unsupported TARGET_AUTH_TYPE: magic"
