from utils.test_types import resolve_test_type


def test_explicit_website_type_selects_browser_runtime():
    assert resolve_test_type({"test_type": "website", "agent_card_url": "https://agent/card"}) == "website"


def test_agent_card_selects_http_runtime_by_default():
    assert resolve_test_type({"agent_card_url": "https://agent/card"}) == "agent"


def test_plain_url_selects_website_runtime_by_default():
    assert resolve_test_type({"url": "https://example.test"}) == "website"
