#!/usr/bin/env python3
"""Production Neo4j loader for the Constellation V1 graph."""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from neo4j import GraphDatabase

COMPANY_COLUMNS = {"ticker", "company_name", "universe", "sector", "industry", "description", "description_short"}
PEOPLE_COLUMNS = {"person_id", "person_name"}
RELATIONSHIP_COLUMNS = {"ticker", "company_name", "person_id", "person_name", "role", "role_category", "extraction_time"}
RELATIONSHIP_TYPES = ("CEO_OF", "CFO_OF", "CHAIRMAN_OF", "BOARD_OF", "EXECUTIVE_OF")
REQUIRED_ENV = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")


class LoaderError(ValueError):
    """Raised when input validation, loading, or reconciliation fails."""


@dataclass(frozen=True)
class ValidatedData:
    companies: list[dict[str, str]]
    people: list[dict[str, str]]
    relationships: list[dict[str, str]]
    relationships_by_type: dict[str, list[dict[str, str]]]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_input_path(path_value: str, root: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else root / path


def load_neo4j_env(root: Path) -> dict[str, str]:
    env_path = root / ".env"
    load_dotenv(dotenv_path=env_path, override=False)
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise LoaderError(f"Missing required Neo4j environment variables: {', '.join(missing)}")
    return {name: os.environ[name] for name in REQUIRED_ENV}


def read_csv_rows(path: Path, required_columns: set[str], label: str) -> list[dict[str, str]]:
    if not path.exists():
        raise LoaderError(f"Missing {label} input file: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise LoaderError(f"{label} CSV has no header: {path}")
        missing = sorted(required_columns - set(reader.fieldnames))
        if missing:
            raise LoaderError(f"{label} CSV missing required columns: {', '.join(missing)}")
        return [{key: (value or "") for key, value in row.items()} for row in reader]


def validate_companies(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    tickers: list[str] = []
    cleaned: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=2):
        ticker = row["ticker"]
        if not ticker.strip():
            raise LoaderError(f"Blank company ticker at companies.csv line {index}")
        tickers.append(ticker)
        cleaned.append({
            "ticker": ticker,
            "company_name": row.get("company_name", ""),
            "universe": row.get("universe", ""),
            "sector": row.get("sector", ""),
            "industry": row.get("industry", ""),
            "description_short": row.get("description_short", ""),
        })
    duplicates = sorted(value for value, count in Counter(tickers).items() if count > 1)
    if duplicates:
        raise LoaderError(f"Duplicate company ticker values: {', '.join(duplicates[:10])}")
    return cleaned


def validate_people(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    ids: list[str] = []
    cleaned: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=2):
        person_id = row["person_id"]
        if not person_id.strip():
            raise LoaderError(f"Blank person_id at people.csv line {index}")
        ids.append(person_id)
        cleaned.append({"person_id": person_id, "person_name": row.get("person_name", "")})
    duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise LoaderError(f"Duplicate person_id values: {', '.join(duplicates[:10])}")
    return cleaned


def map_relationship_type(role: str, role_category: str) -> str:
    if role == "CEO":
        return "CEO_OF"
    if role == "CFO":
        return "CFO_OF"
    if role_category == "BOARD" and "chairman" in role.lower():
        return "CHAIRMAN_OF"
    if role_category == "BOARD":
        return "BOARD_OF"
    if role_category == "EXECUTIVE":
        return "EXECUTIVE_OF"
    raise LoaderError(f"Unsupported or blank role_category: {role_category!r}")


def relationship_identity(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (row["person_id"], row["ticker"], row["relationship_type"], row["role"], row["role_category"])


def validate_relationships(rows: list[dict[str, str]], companies: list[dict[str, str]], people: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, list[dict[str, str]]]]:
    company_tickers = {row["ticker"] for row in companies}
    person_ids = {row["person_id"] for row in people}
    mapped: list[dict[str, str]] = []
    grouped: dict[str, list[dict[str, str]]] = {rel_type: [] for rel_type in RELATIONSHIP_TYPES}
    for index, row in enumerate(rows, start=2):
        ticker = row["ticker"]
        person_id = row["person_id"]
        if ticker not in company_tickers:
            raise LoaderError(f"Relationship line {index} references missing Company ticker: {ticker}")
        if person_id not in person_ids:
            raise LoaderError(f"Relationship line {index} references missing Person person_id: {person_id}")
        rel_type = map_relationship_type(row.get("role", ""), row.get("role_category", ""))
        rel = {
            "ticker": ticker,
            "person_id": person_id,
            "relationship_type": rel_type,
            "role": row.get("role", ""),
            "role_category": row.get("role_category", ""),
            "extraction_time": row.get("extraction_time", ""),
        }
        mapped.append(rel)
        grouped[rel_type].append(rel)
    return mapped, grouped


def validate_inputs(companies_path: Path, people_path: Path, relationships_path: Path) -> ValidatedData:
    companies = validate_companies(read_csv_rows(companies_path, COMPANY_COLUMNS, "companies"))
    people = validate_people(read_csv_rows(people_path, PEOPLE_COLUMNS, "people"))
    relationships, grouped = validate_relationships(read_csv_rows(relationships_path, RELATIONSHIP_COLUMNS, "relationships"), companies, people)
    return ValidatedData(companies, people, relationships, grouped)


def batched(items: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def run_write_batches(driver: Any, database: str, query: str, rows: list[dict[str, str]], batch_size: int) -> int:
    written = 0
    with driver.session(database=database) as session:
        for batch in batched(rows, batch_size):
            result = session.execute_write(lambda tx, b: tx.run(query, rows=b).consume(), batch)
            counters = getattr(result, "counters", None)
            if counters is None:
                raise LoaderError("Batch write did not return Neo4j counters")
            written += len(batch)
    return written


def verify_connectivity(driver: Any) -> None:
    driver.verify_connectivity()


def create_constraints(driver: Any, database: str) -> None:
    queries = [
        "CREATE CONSTRAINT company_ticker_unique IF NOT EXISTS FOR (c:Company) REQUIRE c.ticker IS UNIQUE",
        "CREATE CONSTRAINT person_id_unique IF NOT EXISTS FOR (p:Person) REQUIRE p.person_id IS UNIQUE",
    ]
    with driver.session(database=database) as session:
        for query in queries:
            session.execute_write(lambda tx, q: tx.run(q).consume(), query)


def reset_constellation_graph(driver: Any, database: str, batch_size: int) -> None:
    query = """
    MATCH (n)
    WHERE n:Company OR n:Person
    WITH n LIMIT $limit
    DETACH DELETE n
    RETURN count(n) AS deleted
    """
    while True:
        with driver.session(database=database) as session:
            deleted = session.execute_write(lambda tx: tx.run(query, limit=batch_size).single()["deleted"])
        if deleted == 0:
            break


COMPANY_QUERY = """
UNWIND $rows AS row
MERGE (c:Company {ticker: row.ticker})
SET c.company_name = row.company_name,
    c.universe = row.universe,
    c.sector = row.sector,
    c.industry = row.industry,
    c.description_short = row.description_short
"""

PERSON_QUERY = """
UNWIND $rows AS row
MERGE (p:Person {person_id: row.person_id})
SET p.person_name = row.person_name
"""

RELATIONSHIP_QUERIES = {
    rel_type: f"""
UNWIND $rows AS row
MATCH (p:Person {{person_id: row.person_id}})
MATCH (c:Company {{ticker: row.ticker}})
MERGE (p)-[r:{rel_type} {{role: row.role, role_category: row.role_category}}]->(c)
SET r.extraction_time = row.extraction_time
""" for rel_type in RELATIONSHIP_TYPES
}


def run_qa(driver: Any, database: str) -> dict[str, int]:
    queries = {
        "companies": "MATCH (c:Company) RETURN count(c) AS value",
        "people": "MATCH (p:Person) RETURN count(p) AS value",
        "relationships": "MATCH (:Person)-[r]->(:Company) WHERE type(r) IN $types RETURN count(r) AS value",
        "missing_endpoints": "MATCH ()-[r]->() WHERE type(r) IN $types AND (NOT startNode(r):Person OR NOT endNode(r):Company) RETURN count(r) AS value",
        "duplicate_companies": "MATCH (c:Company) WITH c.ticker AS k, count(*) AS n WHERE n > 1 RETURN count(*) AS value",
        "duplicate_people": "MATCH (p:Person) WITH p.person_id AS k, count(*) AS n WHERE n > 1 RETURN count(*) AS value",
    }
    with driver.session(database=database) as session:
        stats = {name: session.run(query, types=list(RELATIONSHIP_TYPES)).single()["value"] for name, query in queries.items()}
        for rel_type in RELATIONSHIP_TYPES:
            stats[rel_type] = session.run(f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS value").single()["value"]
    return stats


def reconcile(data: ValidatedData, stats: dict[str, int]) -> None:
    failures = []
    if stats["companies"] != len(data.companies):
        failures.append(f"Neo4j Company nodes {stats['companies']} != input companies {len(data.companies)}")
    if stats["people"] != len(data.people):
        failures.append(f"Neo4j Person nodes {stats['people']} != input people {len(data.people)}")
    if stats["relationships"] != len(data.relationships):
        failures.append(f"Neo4j total relationships {stats['relationships']} != input relationships {len(data.relationships)}")
    if stats["missing_endpoints"] != 0:
        failures.append(f"Missing relationship endpoints {stats['missing_endpoints']} != 0")
    if stats["duplicate_companies"] != 0:
        failures.append(f"Duplicate Company ticker groups {stats['duplicate_companies']} != 0")
    if stats["duplicate_people"] != 0:
        failures.append(f"Duplicate Person person_id groups {stats['duplicate_people']} != 0")
    if sum(stats[rel_type] for rel_type in RELATIONSHIP_TYPES) != stats["relationships"]:
        failures.append("Sum of fixed relationship-type counts does not equal total relationships")
    if failures:
        raise LoaderError("Reconciliation failed: " + "; ".join(failures))


def print_line(label: str, value: Any) -> None:
    print(f"{label:<28}: {value}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Constellation V1 CSV data into Neo4j.")
    parser.add_argument("--companies", default="data/companies.csv")
    parser.add_argument("--people", default="data/people.csv")
    parser.add_argument("--relationships", default="data/relationships.csv")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        raise LoaderError("--batch-size must be a positive integer")
    return args


def main(argv: list[str] | None = None) -> int:
    driver = None
    try:
        root = repo_root()
        args = parse_args(argv)
        data = validate_inputs(resolve_input_path(args.companies, root), resolve_input_path(args.people, root), resolve_input_path(args.relationships, root))
        print_line("Validated companies", len(data.companies))
        print_line("Validated people", len(data.people))
        print_line("Validated relationships", len(data.relationships))
        for rel_type in RELATIONSHIP_TYPES:
            print_line(rel_type, len(data.relationships_by_type[rel_type]))
        if args.dry_run:
            print_line("Dry run", "PASS - Neo4j not contacted")
            return 0

        env = load_neo4j_env(root)
        driver = GraphDatabase.driver(env["NEO4J_URI"], auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]))
        verify_connectivity(driver)
        print_line("Neo4j connectivity", "OK")
        create_constraints(driver, args.database)
        print_line("Constraints", "OK")
        if args.reset:
            print("WARNING: --reset active; deleting only Company/Person nodes and connected relationships.")
            reset_constellation_graph(driver, args.database, args.batch_size)
            print_line("Reset", "OK")
        print_line("Company nodes loaded", run_write_batches(driver, args.database, COMPANY_QUERY, data.companies, args.batch_size))
        print_line("Person nodes loaded", run_write_batches(driver, args.database, PERSON_QUERY, data.people, args.batch_size))
        rel_loaded = 0
        for rel_type in RELATIONSHIP_TYPES:
            rel_loaded += run_write_batches(driver, args.database, RELATIONSHIP_QUERIES[rel_type], data.relationships_by_type[rel_type], args.batch_size)
        print_line("Relationships loaded", rel_loaded)
        stats = run_qa(driver, args.database)
        print_line("Input companies", len(data.companies))
        print_line("Input people", len(data.people))
        print_line("Input relationships", len(data.relationships))
        print_line("Neo4j Company nodes", stats["companies"])
        print_line("Neo4j Person nodes", stats["people"])
        print_line("Neo4j total relationships", stats["relationships"])
        for rel_type in RELATIONSHIP_TYPES:
            print_line(rel_type, stats[rel_type])
        print_line("Missing relationship endpoints", stats["missing_endpoints"])
        print_line("Duplicate Company ticker groups", stats["duplicate_companies"])
        print_line("Duplicate Person person_id groups", stats["duplicate_people"])
        reconcile(data, stats)
        print_line("Reconciliation", "PASS")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
