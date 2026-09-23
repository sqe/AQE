#!/usr/bin/env python3
"""Run each generated agent suite against the target recorded in its metadata."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


CATALOG = Path("generated-tests/agent")
SERVICE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error = process.stderr.read().strip() if process.stderr else ""
            raise RuntimeError(f"kubectl port-forward exited before becoming ready: {error}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for local port {port}")


def target_environment(metadata_path: Path) -> tuple[dict[str, str], subprocess.Popen[str] | None]:
    metadata = json.loads(metadata_path.read_text())
    card_url = metadata.get("target_agent", {}).get("card_url")
    if not isinstance(card_url, str) or not card_url:
        raise ValueError(f"{metadata_path} does not declare target_agent.card_url")

    parsed = urlsplit(card_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{metadata_path} has an invalid target Agent Card URL")

    environment = os.environ.copy()
    process = None
    if parsed.scheme == "http" and SERVICE_NAME.fullmatch(parsed.hostname):
        remote_port = parsed.port or 80
        local_port = available_port()
        namespace = environment.get("AQE_NAMESPACE", "aqe")
        process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                namespace,
                "port-forward",
                "--address",
                "127.0.0.1",
                f"service/{parsed.hostname}",
                f"{local_port}:{remote_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for_port(local_port, process)
        target_origin = f"http://127.0.0.1:{local_port}"
        environment["AGENT_CARD_URL"] = f"{target_origin}{parsed.path or '/agent_card'}"
        environment["AGENT_BASE_URL"] = target_origin
    else:
        environment["AGENT_CARD_URL"] = card_url
        environment["AGENT_BASE_URL"] = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return environment, process


def main() -> int:
    test_files = sorted(CATALOG.rglob("test_*.py"))
    for test_file in test_files:
        metadata_path = test_file.with_suffix(".json")
        if not metadata_path.is_file():
            raise FileNotFoundError(f"missing catalog metadata for {test_file}")
        environment, forward = target_environment(metadata_path)
        try:
            print(f"\n==> {test_file} -> {environment['AGENT_BASE_URL']}", flush=True)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(test_file)],
                env=environment,
                check=False,
            )
            if result.returncode:
                return result.returncode
        finally:
            if forward is not None:
                forward.terminate()
                try:
                    forward.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    forward.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
