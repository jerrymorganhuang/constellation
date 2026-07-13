"""Neo4j access layer for the Constellation V1 graph."""
from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from .config import Settings
from .schemas import RELATIONSHIP_TYPES, company_node, person_node, relationship_edge, search_result

REL_PATTERN = "|".join(RELATIONSHIP_TYPES)


class GraphService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

    def close(self) -> None:
        self.driver.close()

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def _read(self, query: str, **params: Any) -> list[Any]:
        with self.driver.session(database=self.settings.neo4j_database) as session:
            return list(session.run(query, **params))

    @staticmethod
    def _graph(records: list[Any]) -> dict[str, list[dict[str, Any]]]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}
        for record in records:
            p = dict(record["p"])
            c = dict(record["c"])
            rel = dict(record["r"])
            rtype = record["relationship"]
            pn = person_node(p)
            cn = company_node(c)
            edge = relationship_edge(p, c, rel, rtype)
            nodes[pn["data"]["id"]] = pn
            nodes[cn["data"]["id"]] = cn
            edges[edge["data"]["id"]] = edge
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    @staticmethod
    def _full_graph(
        company_records: list[Any], person_records: list[Any], relationship_records: list[Any]
    ) -> dict[str, list[dict[str, Any]]]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}

        for record in company_records:
            node = company_node(dict(record["c"]))
            nodes[node["data"]["id"]] = node

        for record in person_records:
            node = person_node(dict(record["p"]))
            nodes[node["data"]["id"]] = node

        for record in relationship_records:
            p = dict(record["p"])
            c = dict(record["c"])
            rel = dict(record["r"])
            rtype = record["relationship"]
            edge = relationship_edge(p, c, rel, rtype)
            edges[edge["data"]["id"]] = edge

        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    def health(self) -> dict[str, int | str]:
        query = """
        MATCH (c:Company) WITH count(c) AS companies
        MATCH (p:Person) WITH companies, count(p) AS people
        MATCH (:Person)-[r:CEO_OF|CFO_OF|CHAIRMAN_OF|BOARD_OF|EXECUTIVE_OF]->(:Company)
        RETURN companies, people, count(r) AS relationships
        """
        row = self._read(query)[0]
        return {"status": "ok", "companies": row["companies"], "people": row["people"], "relationships": row["relationships"]}

    def full_graph(self) -> dict[str, list[dict[str, Any]]]:
        companies = self._read("MATCH (c:Company) RETURN c")
        people = self._read("MATCH (p:Person) RETURN p")
        relationships = self._read(
            f"MATCH (p:Person)-[r:{REL_PATTERN}]->(c:Company) RETURN p, r, c, type(r) AS relationship"
        )
        return self._full_graph(companies, people, relationships)

    def company_graph(self, ticker: str, radius: int) -> dict[str, list[dict[str, Any]]]:
        if radius == 1:
            query = f"MATCH (p:Person)-[r:{REL_PATTERN}]->(c:Company {{ticker: $ticker}}) RETURN p, r, c, type(r) AS relationship"
        else:
            query = f"""
            MATCH (start:Company {{ticker: $ticker}})<-[:{REL_PATTERN}]-(p:Person)
            MATCH (p)-[r:{REL_PATTERN}]->(c:Company)
            RETURN DISTINCT p, r, c, type(r) AS relationship
            """
        return self._graph(self._read(query, ticker=ticker))

    def person_graph(self, person_id: str, radius: int) -> dict[str, list[dict[str, Any]]]:
        if radius == 1:
            query = f"MATCH (p:Person {{person_id: $person_id}})-[r:{REL_PATTERN}]->(c:Company) RETURN p, r, c, type(r) AS relationship"
        else:
            query = f"""
            MATCH (start:Person {{person_id: $person_id}})-[:{REL_PATTERN}]->(first_hop:Company)
            MATCH (p:Person)-[r:{REL_PATTERN}]->(c:Company)
            WHERE c = first_hop
            RETURN DISTINCT p, r, c, type(r) AS relationship
            """
        return self._graph(self._read(query, person_id=person_id))

    def cross_company_graph(self) -> dict[str, list[dict[str, Any]]]:
        query = f"""
        MATCH (p:Person)-[:{REL_PATTERN}]->(c:Company)
        WITH p, count(DISTINCT c) AS company_count
        WHERE company_count > 1
        MATCH (p)-[r:{REL_PATTERN}]->(c:Company)
        RETURN p, r, c, type(r) AS relationship
        """
        return self._graph(self._read(query))

    def search(self, q: str) -> list[dict[str, Any]]:
        query = """
        WITH toLower($q) AS q, $q AS raw
        CALL {
          WITH q, raw
          MATCH (c:Company)
          WHERE toLower(c.ticker) = q OR toLower(c.ticker) STARTS WITH q OR toLower(c.company_name) CONTAINS q
          RETURN 'Company' AS kind, c AS item,
            CASE WHEN toLower(c.ticker) = q THEN 0 WHEN toLower(c.ticker) STARTS WITH q THEN 1 ELSE 2 END AS rank
          UNION ALL
          WITH q
          MATCH (p:Person)
          WHERE toLower(p.person_name) CONTAINS q OR toLower(p.person_id) CONTAINS q
          RETURN 'Person' AS kind, p AS item, 3 AS rank
        }
        RETURN kind, item
        ORDER BY rank, coalesce(item.ticker, item.person_name, item.person_id)
        LIMIT 25
        """
        return [search_result(row["kind"], dict(row["item"])) for row in self._read(query, q=q)]
