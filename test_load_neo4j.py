import csv
from pathlib import Path

import pytest

from scripts import load_neo4j


def company(ticker="ACME"):
    return {
        "ticker": ticker,
        "company_name": "Acme",
        "universe": "test",
        "sector": "Industrials",
        "industry": "Widgets",
        "description": "full text omitted",
        "description_short": "short",
    }


def person(person_id="person-1"):
    return {"person_id": person_id, "person_name": "Ada Lovelace"}


def relationship(role="CEO", role_category="EXECUTIVE", ticker="ACME", person_id="person-1"):
    return {
        "ticker": ticker,
        "company_name": "Acme",
        "person_id": person_id,
        "person_name": "Ada Lovelace",
        "role": role,
        "role_category": role_category,
        "extraction_time": "2026-01-01T00:00:00Z",
    }


def write_csv(path: Path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_valid_inputs(tmp_path: Path):
    companies = tmp_path / "companies.csv"
    people = tmp_path / "people.csv"
    relationships = tmp_path / "relationships.csv"
    write_csv(companies, [company()], sorted(load_neo4j.COMPANY_COLUMNS))
    write_csv(people, [person()], sorted(load_neo4j.PEOPLE_COLUMNS))
    write_csv(relationships, [relationship()], sorted(load_neo4j.RELATIONSHIP_COLUMNS))
    return companies, people, relationships


def test_exact_ceo_maps_to_ceo_of():
    assert load_neo4j.map_relationship_type("CEO", "EXECUTIVE") == "CEO_OF"


def test_exact_cfo_maps_to_cfo_of():
    assert load_neo4j.map_relationship_type("CFO", "EXECUTIVE") == "CFO_OF"


def test_board_chairman_maps_to_chairman_of():
    assert load_neo4j.map_relationship_type("Independent Chairman", "BOARD") == "CHAIRMAN_OF"


def test_other_board_maps_to_board_of():
    assert load_neo4j.map_relationship_type("Director", "BOARD") == "BOARD_OF"


def test_other_executive_maps_to_executive_of():
    assert load_neo4j.map_relationship_type("Chief Legal Officer", "EXECUTIVE") == "EXECUTIVE_OF"


def test_unsupported_role_category_rejected():
    with pytest.raises(load_neo4j.LoaderError, match="Unsupported or blank role_category"):
        load_neo4j.map_relationship_type("Advisor", "ADVISOR")


def test_blank_ticker_rejected():
    with pytest.raises(load_neo4j.LoaderError, match="Blank company ticker"):
        load_neo4j.validate_companies([company(" ")])


def test_blank_person_id_rejected():
    with pytest.raises(load_neo4j.LoaderError, match="Blank person_id"):
        load_neo4j.validate_people([person(" ")])


def test_duplicate_ticker_rejected():
    with pytest.raises(load_neo4j.LoaderError, match="Duplicate company ticker"):
        load_neo4j.validate_companies([company("ACME"), company("ACME")])


def test_duplicate_person_id_rejected():
    with pytest.raises(load_neo4j.LoaderError, match="Duplicate person_id"):
        load_neo4j.validate_people([person("p1"), person("p1")])


def test_missing_company_endpoint_rejected():
    companies = load_neo4j.validate_companies([company("ACME")])
    people = load_neo4j.validate_people([person("p1")])
    with pytest.raises(load_neo4j.LoaderError, match="missing Company"):
        load_neo4j.validate_relationships([relationship(ticker="MISSING", person_id="p1")], companies, people)


def test_missing_person_endpoint_rejected():
    companies = load_neo4j.validate_companies([company("ACME")])
    people = load_neo4j.validate_people([person("p1")])
    with pytest.raises(load_neo4j.LoaderError, match="missing Person"):
        load_neo4j.validate_relationships([relationship(ticker="ACME", person_id="missing")], companies, people)


def test_dry_run_does_not_initialize_neo4j_driver(tmp_path, monkeypatch):
    companies, people, relationships = write_valid_inputs(tmp_path)

    class DriverSentinel:
        @staticmethod
        def driver(*args, **kwargs):
            raise AssertionError("Neo4j driver should not be initialized during dry-run")

    monkeypatch.setattr(load_neo4j, "GraphDatabase", DriverSentinel)
    code = load_neo4j.main([
        "--companies", str(companies),
        "--people", str(people),
        "--relationships", str(relationships),
        "--dry-run",
    ])
    assert code == 0


def test_explicit_env_path_handling(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEO4J_URI=bolt://example:7687\nNEO4J_USER=neo4j\nNEO4J_PASSWORD=secret\n", encoding="utf-8")
    for key in load_neo4j.REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    loaded = load_neo4j.load_neo4j_env(tmp_path)
    assert loaded == {
        "NEO4J_URI": "bolt://example:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "secret",
    }


def test_relationship_grouping_by_fixed_type():
    companies = load_neo4j.validate_companies([company("A"), company("B"), company("C"), company("D"), company("E")])
    people = load_neo4j.validate_people([person("p1"), person("p2"), person("p3"), person("p4"), person("p5")])
    rows = [
        relationship("CEO", "EXECUTIVE", "A", "p1"),
        relationship("CFO", "EXECUTIVE", "B", "p2"),
        relationship("Executive Chairman", "BOARD", "C", "p3"),
        relationship("Director", "BOARD", "D", "p4"),
        relationship("Chief Operating Officer", "EXECUTIVE", "E", "p5"),
    ]
    mapped, grouped = load_neo4j.validate_relationships(rows, companies, people)
    assert [row["relationship_type"] for row in mapped] == list(load_neo4j.RELATIONSHIP_TYPES)
    assert {key: len(value) for key, value in grouped.items()} == {key: 1 for key in load_neo4j.RELATIONSHIP_TYPES}


def test_idempotent_relationship_identity_fields_are_deterministic():
    row = relationship("Director", "BOARD")
    companies = load_neo4j.validate_companies([company()])
    people = load_neo4j.validate_people([person()])
    first, _ = load_neo4j.validate_relationships([row], companies, people)
    second, _ = load_neo4j.validate_relationships([dict(row, extraction_time="later")], companies, people)
    assert load_neo4j.relationship_identity(first[0]) == load_neo4j.relationship_identity(second[0])
    assert load_neo4j.relationship_identity(first[0]) == ("person-1", "ACME", "BOARD_OF", "Director", "BOARD")


def test_company_optional_blanks_are_empty_strings_and_description_is_omitted():
    cleaned = load_neo4j.validate_companies([dict(company(), sector="", description="do not store")])
    assert cleaned[0]["sector"] == ""
    assert "description" not in cleaned[0]
