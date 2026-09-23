#!/usr/bin/env python3
"""Fail-closed checks for candidate promotion evidence."""

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def verify_deployments(payload: dict, expected_tag: str) -> None:
    deployments = payload.get("items", [])
    if not deployments:
        raise SystemExit("candidate namespace has no deployments")
    wrong = []
    unavailable = []
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        images = [container["image"] for container in deployment["spec"]["template"]["spec"]["containers"]]
        if any(not image.endswith(f":{expected_tag}") for image in images):
            wrong.append({"deployment": name, "images": images})
        if deployment.get("status", {}).get("availableReplicas", 0) < deployment["spec"].get("replicas", 1):
            unavailable.append(name)
    if wrong or unavailable:
        raise SystemExit(f"candidate mismatch: wrong_images={wrong}, unavailable={unavailable}")


def verify_model(payload: dict) -> None:
    provider = payload.get("llm_provider", {})
    if provider.get("status") != "connected" or not provider.get("configured"):
        raise SystemExit(f"generation model is not connected: {provider}")


def verify_fleet(payload: dict) -> None:
    if payload.get("status") != "COMPLETED":
        raise SystemExit(f"fleet workflow is not completed: {payload.get('status')}")
    result = payload.get("result") or {}
    summary = result.get("summary") or {}
    runs = result.get("runs") or []
    incomplete = [run for run in runs if (run.get("result") or {}).get("status") == "REQUIREMENTS_NEEDED"]
    uncataloged = [
        run
        for run in runs
        if run.get("successful") and not (run.get("result") or {}).get("test_catalog", {}).get("commit_url")
    ]
    if not result.get("successful") or summary.get("failed", 0) or incomplete or uncataloged:
        raise SystemExit(
            f"fleet qualification failed: successful={result.get('successful')}, "
            f"failed={summary.get('failed')}, requirements_needed={len(incomplete)}, "
            f"uncataloged={len(uncataloged)}"
        )


def catalog_commits(payload: dict) -> list[str]:
    commits = []
    for run in (payload.get("result") or {}).get("runs") or []:
        url = (run.get("result") or {}).get("test_catalog", {}).get("commit_url", "")
        commit = url.rstrip("/").rsplit("/", 1)[-1]
        if len(commit) == 40 and all(character in "0123456789abcdef" for character in commit.lower()):
            commits.append(commit)
    return commits


def main() -> None:
    mode, path, *rest = sys.argv[1:]
    payload = load(path)
    if mode == "deployments":
        verify_deployments(payload, rest[0])
    elif mode == "model":
        verify_model(payload)
    elif mode == "fleet":
        verify_fleet(payload)
    elif mode == "catalog-commits":
        print("\n".join(catalog_commits(payload)))
    else:
        raise SystemExit(f"unknown verification mode: {mode}")


if __name__ == "__main__":
    main()
