"""Low-cardinality Prometheus instrumentation for AQE ASGI services."""

from __future__ import annotations

import re
import time
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


REQUESTS = Counter(
    "aqe_http_requests_total",
    "AQE HTTP requests",
    ("service", "method", "path", "status"),
)
LATENCY = Histogram(
    "aqe_http_request_duration_seconds",
    "AQE HTTP request latency",
    ("service", "method", "path"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
IN_PROGRESS = Gauge(
    "aqe_http_requests_in_progress",
    "AQE HTTP requests currently executing",
    ("service",),
)
CACHE_OPERATIONS = Counter(
    "aqe_cache_operations_total",
    "AQE cache operations",
    ("service", "operation", "result"),
)

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}$")


def normalized_path(path: str) -> str:
    """Replace unbounded path identifiers while retaining useful route shape."""
    segments = path.split("/")
    return "/".join(
        ":id" if segment.isdigit() or _UUID.match(segment) or len(segment) > 48 else segment
        for segment in segments
    ) or "/"


class PrometheusMiddleware:
    def __init__(self, app: Any, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", "/"))
        if path == "/metrics":
            body = generate_latest()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", CONTENT_TYPE_LATEST.encode()),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        method = str(scope.get("method", "UNKNOWN"))
        route = normalized_path(path)
        status = 500
        started = time.perf_counter()
        IN_PROGRESS.labels(self.service).inc()

        async def observe(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            IN_PROGRESS.labels(self.service).dec()
            REQUESTS.labels(self.service, method, route, str(status)).inc()
            LATENCY.labels(self.service, method, route).observe(time.perf_counter() - started)
