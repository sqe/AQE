"""Load, validate, select, and flatten AQE's versioned agent ontology."""

from __future__ import annotations

import json
import re
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
                    f"Agent archetype {archetype['id']} ({archetype.get('display_name', archetype['id'])}). "
                    f"Signals: {', '.join(archetype['signals'])}. "
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


def _evidence_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_evidence_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_evidence_text(item) for item in value)
    return str(value) if value is not None else ""


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _evidence_text(value).lower()).strip()


def classify_agent(evidence: dict[str, Any]) -> dict[str, Any]:
    """Classify an Agent Card from declared metadata without model inference."""
    ontology = load_ontology()
    by_id = {item["id"]: item for item in ontology["archetypes"]}
    declared_ontology = evidence.get("ontology")
    declared = evidence.get("agent_archetype") or evidence.get("archetype")
    if not declared and isinstance(declared_ontology, dict):
        declared = declared_ontology.get("archetype")
    if declared in by_id:
        archetype = by_id[str(declared)]
        return {
            "archetype": archetype["id"],
            "label": archetype.get("display_name", archetype["id"]),
            "product_name": archetype.get("product_name"),
            "confidence": 1.0,
            "matched_signals": [],
            "source": "declared",
        }

    searchable = f" {_normalized(evidence)} "
    matches: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for archetype in ontology["archetypes"]:
        signals = [signal for signal in archetype["signals"] if f" {_normalized(signal)} " in searchable]
        if signals:
            # Prefer multiple independent signals, then more specific phrases.
            specificity = sum(len(_normalized(signal).split()) for signal in signals)
            matches.append((len(signals), specificity, archetype, signals))
    if not matches:
        return {
            "archetype": None,
            "label": "Unclassified",
            "product_name": None,
            "confidence": 0.0,
            "matched_signals": [],
            "source": "none",
        }
    count, specificity, archetype, signals = max(matches, key=lambda item: (item[0], item[1], -ontology["archetypes"].index(item[2])))
    return {
        "archetype": archetype["id"],
        "label": archetype.get("display_name", archetype["id"]),
        "product_name": archetype.get("product_name"),
        "confidence": min(0.95, round(0.55 + 0.1 * count + 0.02 * specificity, 2)),
        "matched_signals": signals,
        "source": "card_evidence",
    }


def discover_fleet_patterns(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a dynamic, non-authoritative family overlay from observed agents."""
    families: dict[str, dict[str, Any]] = {}
    emergent_members: dict[str, set[str]] = {}
    emergent_skills: dict[str, set[str]] = {}
    for agent in agents:
        identity = str(agent.get("identity") or agent.get("name") or "unknown")
        archetype = agent.get("archetype")
        if archetype:
            key = f"canonical:{archetype}"
            family = families.setdefault(
                key,
                {
                    "id": key,
                    "archetype": archetype,
                    "label": agent.get("ontology_label") or archetype,
                    "product_name": agent.get("ontology_product_name"),
                    "kind": "canonical",
                    "members": [],
                    "capabilities": [],
                    "review_required": False,
                },
            )
            family["members"].append(identity)
            family["capabilities"].extend(agent.get("skills", []))
            continue
        for skill in agent.get("skills", []):
            normalized = _normalized(skill)
            prefix = normalized.split()[0] if normalized else ""
            if prefix:
                emergent_members.setdefault(prefix, set()).add(identity)
                emergent_skills.setdefault(prefix, set()).add(str(skill))
    for prefix, members in emergent_members.items():
        if len(members) < 2:
            continue
        key = f"emergent:{prefix}"
        families[key] = {
            "id": key,
            "archetype": None,
            "label": f"Candidate {prefix} family",
            "product_name": None,
            "kind": "emergent",
            "members": sorted(members),
            "capabilities": sorted(emergent_skills[prefix]),
            "review_required": True,
        }
    for family in families.values():
        family["members"] = sorted(set(family["members"]))
        family["capabilities"] = sorted(set(family["capabilities"]))
    return sorted(families.values(), key=lambda family: family["id"])


def select_ontology_context(evidence: dict[str, Any]) -> list[str]:
    """Select applicable ontology records deterministically from supplied target evidence."""
    ontology = load_ontology()
    records = ontology_records(ontology)
    selected = [records[0]["text"]]
    classification = classify_agent(evidence)
    if classification["archetype"]:
        selected.append(next(record["text"] for record in records if record["id"] == classification["archetype"]))
    risk_values = {str(value).lower() for value in evidence.get("risk_labels", [])}
    for dimension in ("autonomy", "data_sensitivity", "impact"):
        value = evidence.get(dimension)
        if value:
            risk_values.add(str(value).lower())
    if evidence.get("network_scope") == "external":
        risk_values.add("external")
    for overlay in ontology["risk_overlays"]:
        if risk_values.intersection(overlay["when"]):
            selected.append(next(record["text"] for record in records if record["id"] == overlay["id"]))
    return selected
