"""Load, validate, select, and flatten AQE's versioned agent ontology."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ONTOLOGY_PATH = Path(__file__).resolve().parents[1] / "ontology" / "agent_ontology.json"


def load_ontology(path: Path = ONTOLOGY_PATH) -> dict[str, Any]:
    ontology = json.loads(path.read_text(encoding="utf-8"))
    required = {"ontology_id", "version", "dimensions", "universal_obligations", "archetypes", "risk_overlays"}
    missing = required - ontology.keys()
    if missing:
        raise ValueError(f"Agent ontology is missing: {', '.join(sorted(missing))}")
    archetype_ids = [item["id"] for item in ontology["archetypes"]]
    if len(archetype_ids) != len(set(archetype_ids)):
        raise ValueError("Agent ontology archetype IDs must be unique")
    return ontology


def ontology_records(ontology: dict[str, Any] | None = None) -> list[dict[str, str]]:
    source = ontology or load_ontology()
    records = [
        {
            "id": "universal",
            "kind": "universal_obligations",
            "text": "Universal agent test obligations: " + " ".join(source["universal_obligations"]),
        }
    ]
    for archetype in source["archetypes"]:
        records.append(
            {
                "id": archetype["id"],
                "kind": "archetype",
                "text": (
                    f"Agent archetype {archetype['id']}. Signals: {', '.join(archetype['signals'])}. "
                    f"Quality attributes: {', '.join(archetype['quality_attributes'])}. "
                    f"Required scenarios: {', '.join(archetype['required_scenarios'])}."
                ),
            }
        )
    for overlay in source["risk_overlays"]:
        records.append(
            {
                "id": overlay["id"],
                "kind": "risk_overlay",
                "text": (
                    f"Risk overlay {overlay['id']} when {', '.join(overlay['when'])}. "
                    f"Obligations: {' '.join(overlay['obligations'])}"
                ),
            }
        )
    return records


def select_ontology_context(evidence: dict[str, Any]) -> list[str]:
    """Select applicable ontology records deterministically from supplied target evidence."""
    ontology = load_ontology()
    searchable = json.dumps(evidence, default=str).lower()
    selected = [ontology_records(ontology)[0]["text"]]
    for archetype in ontology["archetypes"]:
        if archetype["id"] == evidence.get("agent_archetype") or any(
            signal in searchable for signal in archetype["signals"]
        ):
            selected.append(next(record["text"] for record in ontology_records(ontology) if record["id"] == archetype["id"]))
    risk_values = {str(value).lower() for value in evidence.get("risk_labels", [])}
    for dimension in ("autonomy", "data_sensitivity", "impact"):
        value = evidence.get(dimension)
        if value:
            risk_values.add(str(value).lower())
    if evidence.get("network_scope") == "external":
        risk_values.add("external")
    for overlay in ontology["risk_overlays"]:
        if risk_values.intersection(overlay["when"]):
            selected.append(next(record["text"] for record in ontology_records(ontology) if record["id"] == overlay["id"]))
    return selected
