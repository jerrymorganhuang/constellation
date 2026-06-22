"""FastAPI backend for the Constellation web app."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

RELATIONSHIP_TYPES = ("CEO_OF", "CFO_OF", "BOARD_OF")
NODE_LABELS = ("Company", "Person")
DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"


def get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def node_type(labels: list[str] | tuple[str, ...]) -> str:
    for label in NODE_LABELS:
        if label in labels:
            return label
    return labels[0] if labels else "Node"


def node_label(properties: dict[str, Any], kind: str) -> str:
    if kind == "Company":
        return properties.get("ticker") or properties.get("company_name") or properties.get("node_id")
    return properties.get("name") or properties.get("normalized_name") or properties.get("node_id")


def cytoscape_node(node: Any) -> dict[str, Any]:
    properties = dict(node)
    kind = node_type(list(node.labels))
    node_id = properties.get("node_id") or str(node.element_id)
    return {
        "data": {
            "id": node_id,
            "label": node_label(properties, kind),
            "type": kind,
            **properties,
        }
    }


def cytoscape_edge(relationship: Any) -> dict[str, Any]:
    properties = dict(relationship)
    source = properties.get("source_node_id") or relationship.start_node.get("node_id")
    target = properties.get("target_node_id") or relationship.end_node.get("node_id")
    relationship_type = relationship.type
    edge_id = f"{source}:{relationship_type}:{target}"
    return {
        "data": {
            "id": edge_id,
            "source": source,
            "target": target,
            "relationship": relationship_type,
            **properties,
        }
    }


def graph_from_records(records: list[Any]) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for record in records:
        for value in record.values():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if hasattr(item, "labels"):
                    node = cytoscape_node(item)
                    nodes[node["data"]["id"]] = node
                elif hasattr(item, "type") and hasattr(item, "start_node"):
                    edge = cytoscape_edge(item)
                    edges[edge["data"]["id"]] = edge
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


@asynccontextmanager
async def lifespan(app: FastAPI):
    uri = get_env("NEO4J_URI", DEFAULT_NEO4J_URI)
    user = get_env("NEO4J_USER", DEFAULT_NEO4J_USER)
    password = get_env("NEO4J_PASSWORD")
    app.state.driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        yield
    finally:
        app.state.driver.close()


CORS_ALLOW_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://34.80.219.177:5173",
]


app = FastAPI(title="Constellation API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def read_graph(query: str, **parameters: Any) -> dict[str, list[dict[str, Any]]]:
    try:
        with app.state.driver.session() as session:
            records = list(session.run(query, **parameters))
        return graph_from_records(records)
    except Neo4jError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with app.state.driver.session() as session:
            session.run("RETURN 1").consume()
        return {"status": "ok"}
    except Neo4jError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/graph/universal")
def universal_graph() -> dict[str, list[dict[str, Any]]]:
    return read_graph(
        """
        MATCH (n)
        WHERE n:Company OR n:Person
        OPTIONAL MATCH (n)-[r:CEO_OF|CFO_OF|BOARD_OF]->(m)
        RETURN collect(DISTINCT n) + collect(DISTINCT m) AS nodes, collect(DISTINCT r) AS edges
        """
    )


@app.get("/search")
def search(q: str = Query(..., min_length=1)) -> dict[str, list[dict[str, Any]]]:
    query = q.strip().lower()
    with app.state.driver.session() as session:
        records = list(
            session.run(
                """
                MATCH (n)
                WHERE (n:Company AND (
                    toLower(coalesce(n.ticker, '')) CONTAINS $q OR
                    toLower(coalesce(n.company_name, '')) CONTAINS $q
                )) OR (n:Person AND toLower(coalesce(n.name, '')) CONTAINS $q)
                RETURN n
                ORDER BY CASE WHEN n:Company THEN 0 ELSE 1 END, coalesce(n.ticker, n.name)
                LIMIT 25
                """,
                q=query,
            )
        )
    return {"results": [cytoscape_node(record["n"])["data"] for record in records]}


@app.get("/graph/company/{ticker}")
def company_graph(ticker: str, radius: int = Query(1, ge=1, le=3)) -> dict[str, list[dict[str, Any]]]:
    return read_graph(
        f"""
        MATCH (c:Company)
        WHERE toLower(c.ticker) = toLower($ticker)
        MATCH path=(c)-[:CEO_OF|CFO_OF|BOARD_OF*1..{radius}]-(n)
        RETURN nodes(path) AS nodes, relationships(path) AS edges
        """,
        ticker=ticker,
    )


@app.get("/graph/person/{name}")
def person_graph(name: str, radius: int = Query(1, ge=1, le=3)) -> dict[str, list[dict[str, Any]]]:
    return read_graph(
        f"""
        MATCH (p:Person)
        WHERE toLower(p.name) = toLower($name)
        MATCH path=(p)-[:CEO_OF|CFO_OF|BOARD_OF*1..{radius}]-(n)
        RETURN nodes(path) AS nodes, relationships(path) AS edges
        """,
        name=unquote(name),
    )


@app.get("/graph/cross-company")
def cross_company_graph() -> dict[str, list[dict[str, Any]]]:
    return read_graph(
        """
        MATCH (p:Person)-[r:CEO_OF|CFO_OF|BOARD_OF]->(c:Company)
        WITH p, collect(DISTINCT c) AS companies, collect(DISTINCT r) AS edges
        WHERE size(companies) > 1
        RETURN collect(DISTINCT p) + companies AS nodes, edges
        """
    )


@app.get("/graph/company-network")
def company_network() -> dict[str, list[dict[str, Any]]]:
    with app.state.driver.session() as session:
        records = list(
            session.run(
                """
                MATCH (c1:Company)<-[:CEO_OF|CFO_OF|BOARD_OF]-(p:Person)-[:CEO_OF|CFO_OF|BOARD_OF]->(c2:Company)
                WHERE c1.node_id < c2.node_id
                WITH c1, c2, collect(DISTINCT p.name) AS shared_people
                RETURN c1, c2, shared_people, size(shared_people) AS shared_count
                ORDER BY shared_count DESC, c1.ticker, c2.ticker
                """
            )
        )
    graph = graph_from_records(records)
    for record in records:
        source = record["c1"].get("node_id")
        target = record["c2"].get("node_id")
        edge_id = f"{source}:SHARES_PERSON:{target}"
        graph["edges"].append(
            {
                "data": {
                    "id": edge_id,
                    "source": source,
                    "target": target,
                    "relationship": "SHARES_PERSON",
                    "shared_people": record["shared_people"],
                    "shared_count": record["shared_count"],
                }
            }
        )
    return graph
