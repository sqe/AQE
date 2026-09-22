from agents.knowledge_ingestion.app import extract_document_text, refine_requirements


def test_markdown_document_text_is_extracted():
    text = extract_document_text("requirements.md", b"The agent must respond within 2 seconds.")

    assert text == "The agent must respond within 2 seconds."


def test_requirements_are_deduplicated():
    requirements = refine_requirements("The agent must answer.\nThe agent must answer.")

    assert len(requirements) == 1


def test_requirement_is_classified_as_testable():
    requirements = refine_requirements("The agent must answer within 2 seconds.")

    assert requirements[0]["testable"] is True
