from utils.agent_ontology import (
    classify_agent,
    discover_fleet_patterns,
    load_ontology,
    ontology_records,
    select_ontology_context,
)


def test_ontology_has_unique_archetypes():
    ontology = load_ontology()

    assert len({item["id"] for item in ontology["archetypes"]}) == len(ontology["archetypes"])


def test_teaching_evidence_selects_teaching_obligations():
    context = select_ontology_context({"skills": ["Teach an adaptive lesson"]})

    assert any("teaching_coaching" in item for item in context)


def test_external_target_selects_network_risk_overlay():
    context = select_ontology_context({"network_scope": "external"})

    assert any("external_network" in item for item in context)


def test_ontology_records_include_universal_obligations():
    records = ontology_records()

    assert records[0]["kind"] == "universal_obligations"


def test_high_impact_agent_selects_regulated_overlay():
    context = select_ontology_context({"impact": "high"})

    assert any("regulated_or_high_impact" in item for item in context)


def test_aqe_test_agent_is_classified_as_ai_buster():
    classification = classify_agent(
        {"name": "aqe-test-generation", "skills": [{"id": "generate_tests"}], "description": "Generates pytest suites"}
    )

    assert classification["archetype"] == "software_quality_engineering"


def test_explicit_archetype_takes_precedence_over_card_signals():
    classification = classify_agent(
        {"agent_archetype": "teaching_coaching", "description": "Generates software tests"}
    )

    assert classification["source"] == "declared"


def test_unrelated_agent_is_not_falsely_classified_as_quality_engineering():
    classification = classify_agent({"name": "weather-agent", "skills": [{"id": "forecast.temperature"}]})

    assert classification["archetype"] is None


def test_ontology_records_include_ai_buster_archetype():
    record = next(item for item in ontology_records() if item["id"] == "software_quality_engineering")

    assert "Software Quality Engineering" in record["text"]


def test_fleet_patterns_group_canonical_quality_agents():
    families = discover_fleet_patterns(
        [
            {
                "identity": "generator",
                "archetype": "software_quality_engineering",
                "ontology_label": "Software Quality Engineering",
                "ontology_product_name": "AI Buster",
                "skills": ["generate_tests"],
            },
            {
                "identity": "executor",
                "archetype": "software_quality_engineering",
                "ontology_label": "Software Quality Engineering",
                "ontology_product_name": "AI Buster",
                "skills": ["qe.validate"],
            },
        ]
    )

    assert families[0]["members"] == ["executor", "generator"]


def test_fleet_patterns_flag_repeated_unknown_skill_namespace_for_review():
    families = discover_fleet_patterns(
        [
            {"identity": "alpha", "skills": ["legal.review"]},
            {"identity": "beta", "skills": ["legal.summarize"]},
        ]
    )

    assert families[0]["review_required"] is True
