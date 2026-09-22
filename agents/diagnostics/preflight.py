"""Fail-fast dependency diagnostics used by CI, Compose, and Argo CD hooks."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class Check:
    name: str
    target: str
    required: bool = True


def _host_port(target: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(target if "://" in target else f"tcp://{target}")
    if not parsed.hostname:
        raise ValueError(f"Invalid target: {target}")
    return parsed.hostname, parsed.port or default_port


async def tcp_check(check: Check, default_port: int) -> tuple[str, bool, str]:
    try:
        host, port = _host_port(check.target, default_port)
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
        writer.close()
        await writer.wait_closed()
        return check.name, True, f"{host}:{port} reachable"
    except (OSError, TimeoutError, ValueError) as exc:
        return check.name, False, str(exc)


async def http_check(check: Check) -> tuple[str, bool, str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(check.target)
        ok = response.status_code < 500
        return check.name, ok, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return check.name, False, str(exc)


async def main() -> int:
    checks = [
        (Check("postgres", os.getenv("POSTGRES_URL", "postgresql://postgres:5432/aqe")), 5432),
        (Check("kafka", os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")), 9092),
        (Check("temporal", os.getenv("TEMPORAL_ADDRESS", "temporal:7233")), 7233),
        (Check("rustfs", os.getenv("OBJECT_STORE_ENDPOINT", "http://rustfs:9000")), 9000),
    ]
    results = await asyncio.gather(*(tcp_check(check, port) for check, port in checks))
    model_health_url = os.getenv("MODEL_HEALTH_URL", "")
    if model_health_url:
        results = [*results, await http_check(Check("model", model_health_url))]
    agent_card_urls = os.getenv("AGENT_CARD_URLS", "")
    for item in agent_card_urls.split(","):
        name, separator, url = item.strip().partition("=")
        if separator and name and url:
            results = [*results, await http_check(Check(f"agent:{name}", url))]

    failed = False
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        failed = failed or not ok
    print(f"host={socket.gethostname()} diagnostics={'failed' if failed else 'passed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
