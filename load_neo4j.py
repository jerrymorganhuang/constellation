#!/usr/bin/env python3
"""Load Constellation V0 CSV outputs into Neo4j."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase


DEFAULT_INPUT_DIR = "data/test_soxx_signature_only"
DEFAULT_URI = "bolt://localhost:7687"
DEFAULT_USER = "neo4j"
DEFAULT_PASSWORD = "constellation123"
RELATIONSHIP_TYPES = ("CEO_OF", "CFO_OF", "BOARD_OF")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load Constellation V0 CSV outputs into Neo4j."
    )
    parser.add_argument(
        "--input-dir", "--data-dir", dest="input_dir", default=DEFAULT_INPUT_DIR
    )
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the existing Neo4j graph before loading CSV data.",
    )
    return parser.parse_args()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Required CSV file not found: {path}")

    with path.open(newline="", encoding="utf-8") as csv_file:
        return [dict(row) for row in csv.DictReader(csv_file)]


def clean_properties(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key and value is not None}


def create_constraints(tx: Any) -> None:
    tx.run(
        "CREATE CONSTRAINT company_node_id_unique IF NOT EXISTS "
        "FOR (c:Company) REQUIRE c.node_id IS UNIQUE"
    )
    tx.run(
        "CREATE CONSTRAINT person_node_id_unique IF NOT EXISTS "
        "FOR (p:Person) REQUIRE p.node_id IS UNIQUE"
    )


def merge_node(tx: Any, label: str, row: dict[str, str]) -> None:
    properties = clean_properties(row)
    node_id = properties.get("node_id")
    if not node_id:
        raise ValueError(f"Cannot load {label} row without node_id: {row}")

    tx.run(
        f"MERGE (n:{label} {{node_id: $node_id}}) SET n += $properties",
        node_id=node_id,
        properties=properties,
    )


def relationship_properties(row: dict[str, str]) -> dict[str, str]:
    keys = ("filing_date", "filing_url", "relationship_source")
    return {key: row[key] for key in keys if row.get(key)}


def merge_relationship(tx: Any, row: dict[str, str]) -> bool:
    relationship_type = row.get("relationship_type", "")
    if relationship_type not in RELATIONSHIP_TYPES:
        return False

    source_node_id = row.get("source_node_id")
    target_node_id = row.get("target_node_id")
    if not source_node_id or not target_node_id:
        raise ValueError(f"Cannot load relationship row without source/target IDs: {row}")

    query = (
        "MATCH (source {node_id: $source_node_id}) "
        "MATCH (target {node_id: $target_node_id}) "
        f"MERGE (source)-[r:{relationship_type}]->(target) "
        "SET r += $properties "
        "RETURN count(r) AS loaded"
    )
    result = tx.run(
        query,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        properties=relationship_properties(row),
    ).single()
    return bool(result and result["loaded"])


def load_graph(input_dir: Path, uri: str, user: str, password: str, clear: bool) -> Counter[str]:
    company_rows = read_csv_rows(input_dir / "company_nodes.csv")
    person_rows = read_csv_rows(input_dir / "person_nodes.csv")
    edge_rows = read_csv_rows(input_dir / "edges.csv")

    summary: Counter[str] = Counter()

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            if clear:
                session.run("MATCH (n) DETACH DELETE n")

            session.execute_write(create_constraints)

            for row in company_rows:
                session.execute_write(merge_node, "Company", row)
                summary["Companies loaded"] += 1

            for row in person_rows:
                session.execute_write(merge_node, "Person", row)
                summary["People loaded"] += 1

            for row in edge_rows:
                relationship_type = row.get("relationship_type", "")
                if session.execute_write(merge_relationship, row):
                    summary[f"{relationship_type} loaded"] += 1
                    summary["Total relationships loaded"] += 1
    finally:
        driver.close()

    return summary


def print_summary(summary: Counter[str]) -> None:
    print("Neo4j load summary")
    print(f"Companies loaded: {summary['Companies loaded']}")
    print(f"People loaded: {summary['People loaded']}")
    for relationship_type in RELATIONSHIP_TYPES:
        print(f"{relationship_type} loaded: {summary[f'{relationship_type} loaded']}")
    print(f"Total relationships loaded: {summary['Total relationships loaded']}")


def main() -> None:
    args = parse_args()
    summary = load_graph(
        input_dir=Path(args.input_dir),
        uri=args.uri,
        user=args.user,
        password=args.password,
        clear=args.clear,
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
