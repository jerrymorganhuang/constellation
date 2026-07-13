from pathlib import Path

import pytest

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


def _write_required_env(env: Path, extra: str = "") -> None:
    env.write_text(
        "NEO4J_URI=bolt://db:7687\n"
        "NEO4J_USER=neo4j\n"
        "NEO4J_PASSWORD=secret\n"
        f"{extra}"
    )


def test_missing_required_variable_raises(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("NEO4J_USER=neo4j\nNEO4J_PASSWORD=secret\n")
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="NEO4J_URI"):
        get_settings(env)


def test_optional_empty_string_default_succeeds(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _write_required_env(env, "CORS_ALLOW_ORIGINS=\n")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)

    settings = get_settings(env)

    assert settings.cors_allow_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


def test_missing_cors_allow_origins_produces_only_localhost_origins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _write_required_env(env)
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)

    settings = get_settings(env)

    assert settings.cors_allow_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


def test_comma_separated_extra_origins_are_trimmed_and_included(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _write_required_env(env, "CORS_ALLOW_ORIGINS= https://one.example,https://two.example , , http://localhost:5173 \n")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)

    settings = get_settings(env)

    assert settings.cors_allow_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://one.example",
        "https://two.example",
    )


def test_full_graph_preserves_isolated_company_and_person_nodes():
    from app.backend.graph_service import GraphService

    service = object.__new__(GraphService)
    calls = []

    company = {"ticker": "NVDA", "company_name": "NVIDIA Corporation"}
    isolated_company = {"ticker": "GSBC", "company_name": "Great Southern Bancorp, Inc."}
    person = {"person_id": "JENSEN_HUANG", "person_name": "Jensen Huang"}
    isolated_person = {"person_id": "ISOLATED_PERSON", "person_name": "Isolated Person"}
    rel = {"role": "CEO", "role_category": "CEO", "extraction_time": "2026-01-01T00:00:00Z"}

    def fake_read(query, **params):
        calls.append(query)
        if query == "MATCH (c:Company) RETURN c":
            return [{"c": company}, {"c": isolated_company}]
        if query == "MATCH (p:Person) RETURN p":
            return [{"p": person}, {"p": isolated_person}]
        if "MATCH (p:Person)-[r:" in query:
            return [{"p": person, "r": rel, "c": company, "relationship": "CEO_OF"}]
        raise AssertionError(f"unexpected query: {query}")

    service._read = fake_read

    graph = service.full_graph()

    node_ids = {node["data"]["id"] for node in graph["nodes"]}
    assert node_ids == {
        "company:NVDA",
        "company:GSBC",
        "person:JENSEN_HUANG",
        "person:ISOLATED_PERSON",
    }
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["data"]["source"] == "person:JENSEN_HUANG"
    assert graph["edges"][0]["data"]["target"] == "company:NVDA"
    assert calls == [
        "MATCH (c:Company) RETURN c",
        "MATCH (p:Person) RETURN p",
        (
            "MATCH (p:Person)-[r:CEO_OF|CFO_OF|CHAIRMAN_OF|BOARD_OF|EXECUTIVE_OF]->(c:Company) "
            "RETURN p, r, c, type(r) AS relationship"
        ),
    ]


def _graph_ids(graph):
    return (
        {node["data"]["id"] for node in graph["nodes"]},
        {edge["data"]["id"] for edge in graph["edges"]},
    )


def _record(person_id, person_name, ticker, company_name, relationship="BOARD_OF", role="Board Member"):
    return {
        "p": {"person_id": person_id, "person_name": person_name},
        "r": {"role": role, "role_category": role, "extraction_time": "2026-01-01T00:00:00Z"},
        "c": {"ticker": ticker, "company_name": company_name},
        "relationship": relationship,
    }


def test_company_radius_2_contains_company_radius_1_nodes_and_edges():
    from app.backend.graph_service import GraphService

    service = object.__new__(GraphService)
    radius_1_records = [
        _record("CEO_ONLY", "CEO Only", "NVDA", "NVIDIA Corporation", "CEO_OF", "CEO"),
        _record("SHARED_EXEC", "Shared Exec", "NVDA", "NVIDIA Corporation"),
    ]
    radius_2_records = [
        *radius_1_records,
        _record("SHARED_EXEC", "Shared Exec", "AMD", "Advanced Micro Devices, Inc."),
    ]

    def fake_read(query, **params):
        assert params == {"ticker": "NVDA"}
        if "RETURN DISTINCT p, r, c" in query:
            assert "MATCH (start:Company {ticker: $ticker})" in query
            assert "MATCH (p)-[r:" in query
            return radius_2_records
        return radius_1_records

    service._read = fake_read

    radius_1_nodes, radius_1_edges = _graph_ids(service.company_graph("NVDA", 1))
    radius_2_nodes, radius_2_edges = _graph_ids(service.company_graph("NVDA", 2))

    assert radius_1_nodes <= radius_2_nodes
    assert radius_1_edges <= radius_2_edges


def test_company_radius_2_keeps_person_connected_only_to_starting_company():
    from app.backend.graph_service import GraphService

    service = object.__new__(GraphService)

    def fake_read(query, **params):
        assert params == {"ticker": "NVDA"}
        assert "RETURN DISTINCT p, r, c" in query
        assert "shared:Person" not in query
        return [
            _record("CEO_ONLY", "CEO Only", "NVDA", "NVIDIA Corporation", "CEO_OF", "CEO"),
            _record("SHARED_EXEC", "Shared Exec", "NVDA", "NVIDIA Corporation"),
            _record("SHARED_EXEC", "Shared Exec", "AMD", "Advanced Micro Devices, Inc."),
        ]

    service._read = fake_read

    graph = service.company_graph("NVDA", 2)
    node_ids, edge_ids = _graph_ids(graph)

    assert "person:CEO_ONLY" in node_ids
    assert "company:NVDA" in node_ids
    assert stable_edge_id("CEO_ONLY", "NVDA", "CEO_OF", "CEO", "CEO") in edge_ids


def test_person_radius_2_contains_person_radius_1_nodes_and_edges():
    from app.backend.graph_service import GraphService

    service = object.__new__(GraphService)
    radius_1_records = [
        _record("JENSEN_HUANG", "Jensen Huang", "NVDA", "NVIDIA Corporation", "CEO_OF", "CEO"),
        _record("JENSEN_HUANG", "Jensen Huang", "AAPL", "Apple Inc."),
    ]
    radius_2_records = [
        *radius_1_records,
        _record("OTHER_EXEC", "Other Exec", "NVDA", "NVIDIA Corporation"),
    ]

    def fake_read(query, **params):
        assert params == {"person_id": "JENSEN_HUANG"}
        if "RETURN DISTINCT p, r, c" in query:
            assert "MATCH (start:Person {person_id: $person_id})" in query
            assert "WHERE c = first_hop" in query
            return radius_2_records
        return radius_1_records

    service._read = fake_read

    radius_1_nodes, radius_1_edges = _graph_ids(service.person_graph("JENSEN_HUANG", 1))
    radius_2_nodes, radius_2_edges = _graph_ids(service.person_graph("JENSEN_HUANG", 2))

    assert radius_1_nodes <= radius_2_nodes
    assert radius_1_edges <= radius_2_edges


def test_radius_validation_accepts_only_1_and_2():
    from fastapi import HTTPException

    from app.backend.main import checked_radius

    assert checked_radius(1) == 1
    assert checked_radius(2) == 2
    for radius in (0, 3):
        with pytest.raises(HTTPException) as exc_info:
            checked_radius(radius)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "radius must be 1 or 2"
