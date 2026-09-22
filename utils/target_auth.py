"""Authentication helpers available to generated agent and website tests.

Only TARGET_AUTH_* values are forwarded into the generated-test subprocess.
The helpers never read AQE's infrastructure or model credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import httpx


@dataclass(frozen=True)
class TargetAuth:
    kind: str = "none"
    token: str = ""
    username: str = ""
    password: str = ""
    api_key: str = ""
    api_key_header: str = "X-API-Key"
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TargetAuth":
        source = os.environ if env is None else env
        value = lambda name, default="": source.get(f"TARGET_AUTH_{name}", default)
        return cls(
            kind=value("TYPE", "none").lower(),
            token=value("TOKEN"),
            username=value("USERNAME"),
            password=value("PASSWORD"),
            api_key=value("API_KEY"),
            api_key_header=value("API_KEY_HEADER", "X-API-Key"),
            token_url=value("TOKEN_URL"),
            client_id=value("CLIENT_ID"),
            client_secret=value("CLIENT_SECRET"),
            scope=value("SCOPE"),
        )


def authenticated_client(
    auth: TargetAuth | None = None,
    *,
    base_url: str = "",
    timeout: float = 30,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Create an HTTP client for none, bearer, basic, API-key, or OAuth2 auth."""
    config = auth or TargetAuth.from_env()
    headers: dict[str, str] = {}
    basic: httpx.BasicAuth | None = None

    if config.kind == "bearer":
        headers["Authorization"] = f"Bearer {config.token}"
    elif config.kind == "basic":
        basic = httpx.BasicAuth(config.username, config.password)
    elif config.kind == "api_key":
        headers[config.api_key_header] = config.api_key
    elif config.kind == "oauth2_client_credentials":
        if not config.token_url:
            raise ValueError("TARGET_AUTH_TOKEN_URL is required for OAuth2 client credentials")
        token_response = httpx.post(
            config.token_url,
            data={"grant_type": "client_credentials", "scope": config.scope},
            auth=(config.client_id, config.client_secret),
            timeout=timeout,
        )
        token_response.raise_for_status()
        headers["Authorization"] = f"Bearer {token_response.json()['access_token']}"
    elif config.kind != "none":
        raise ValueError(f"Unsupported TARGET_AUTH_TYPE: {config.kind}")

    return httpx.Client(
        base_url=base_url,
        headers=headers,
        auth=basic,
        timeout=timeout,
        transport=transport,
    )


def browser_context_options(auth: TargetAuth | None = None) -> dict[str, Any]:
    """Return Playwright context options for HTTP-level website authentication."""
    config = auth or TargetAuth.from_env()
    if config.kind == "basic":
        return {"http_credentials": {"username": config.username, "password": config.password}}
    if config.kind == "bearer":
        return {"extra_http_headers": {"Authorization": f"Bearer {config.token}"}}
    if config.kind == "api_key":
        return {"extra_http_headers": {config.api_key_header: config.api_key}}
    if config.kind in {"none", "form"}:
        return {}
    raise ValueError(f"Unsupported browser TARGET_AUTH_TYPE: {config.kind}")


def login_with_form(page: Any, login_url: str | None = None) -> None:
    """Log into a website using explicit, environment-supplied selectors."""
    env = os.environ
    url = login_url or env.get("TARGET_AUTH_LOGIN_URL", "")
    username_selector = env.get("TARGET_AUTH_USERNAME_SELECTOR", "")
    password_selector = env.get("TARGET_AUTH_PASSWORD_SELECTOR", "")
    submit_selector = env.get("TARGET_AUTH_SUBMIT_SELECTOR", "")
    if not all((url, username_selector, password_selector, submit_selector)):
        raise ValueError("Form auth requires login URL and username/password/submit selectors")
    page.goto(url)
    page.locator(username_selector).fill(env.get("TARGET_AUTH_USERNAME", ""))
    page.locator(password_selector).fill(env.get("TARGET_AUTH_PASSWORD", ""))
    page.locator(submit_selector).click()
    page.wait_for_load_state("networkidle")
