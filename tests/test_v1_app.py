from pathlib import Path

from app.backend.config import get_settings
from app.backend.schemas import (
    RELATIONSHIP_TYPES,
    company_node,
    company_cyto_id,
    person_node,
    person_cyto_id,
    relationship_edge,
    search_result,
    stable_edge_id,
)


def test_company_node_serialization_and_stable_id():
    node = company_node({"ticker": "NVDA", "company_name": "NVIDIA Corporation"})
    assert node["data"]["id"] == "company:NVDA"
    assert node["data"]["label"] == "NVDA"
    assert company_cyto_id("MSFT") == "company:MSFT"


def test_person_node_serialization_and_stable_id():
    node = person_node({"person_id": "JENSEN_HUANG", "person_name": "Jensen Huang"})
    assert node["data"]["id"] == "person:JENSEN_HUANG"
    assert node["data"]["label"] == "Jensen Huang"
    assert person_cyto_id("X") == "person:X"


def test_relationship_serialization_and_stable_edge_id():
    person = {"person_id": "JENSEN_HUANG", "person_name": "Jensen Huang"}
    company = {"ticker": "NVDA", "company_name": "NVIDIA Corporation"}
    rel = {"role": "CEO", "role_category": "CEO", "extraction_time": "2026-01-01T00:00:00Z"}
    edge = relationship_edge(person, company, rel, "CEO_OF")
    assert edge["data"]["source"] == "person:JENSEN_HUANG"
    assert edge["data"]["target"] == "company:NVDA"
    assert edge["data"]["id"] == stable_edge_id("JENSEN_HUANG", "NVDA", "CEO_OF", "CEO", "CEO")
    assert edge["data"]["id"] == stable_edge_id("JENSEN_HUANG", "NVDA", "CEO_OF", "CEO", "CEO")


def test_search_result_serialization():
    assert search_result("Company", {"ticker": "AAPL", "company_name": "Apple Inc."})["id"] == "company:AAPL"
    assert search_result("Person", {"person_id": "TIM_COOK", "person_name": "Tim Cook"})["id"] == "person:TIM_COOK"


def test_all_five_relationship_types_supported():
    assert RELATIONSHIP_TYPES == ("CEO_OF", "CFO_OF", "CHAIRMAN_OF", "BOARD_OF", "EXECUTIVE_OF")


def test_no_legacy_schema_usage_in_application_code():
    legacy = ["node_id", "normalized_name", "source_node_id", "target_node_id", "filing_date", "filing_url", "relationship_source"]
    text = "\n".join(p.read_text() for p in Path("app/backend").glob("*.py"))
    for token in legacy:
        assert token not in text


def test_explicit_env_path_handling(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("NEO4J_URI=bolt://db:7687\nNEO4J_USER=neo4j\nNEO4J_PASSWORD=secret\nNEO4J_DATABASE=constellation\nCORS_ALLOW_ORIGINS=http://vm.local:5173\n")
    monkeypatch.delenv("NEO4J_URI", raising=False)
    settings = get_settings(env)
    assert settings.neo4j_uri == "bolt://db:7687"
    assert settings.neo4j_database == "constellation"
    assert "http://vm.local:5173" in settings.cors_allow_origins
