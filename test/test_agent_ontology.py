from utils.agent_ontology import load_ontology, ontology_records, select_ontology_context


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
