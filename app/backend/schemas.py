"""Serialization helpers for the Constellation V1 graph schema."""
from __future__ import annotations

import hashlib
from typing import Any

RELATIONSHIP_TYPES = ("CEO_OF", "CFO_OF", "CHAIRMAN_OF", "BOARD_OF", "EXECUTIVE_OF")


def company_cyto_id(ticker: str) -> str:
    return f"company:{ticker}"


def person_cyto_id(person_id: str) -> str:
    return f"person:{person_id}"


def stable_edge_id(person_id: str, ticker: str, relationship: str, role: str | None, role_category: str | None) -> str:
    raw = "\x1f".join([person_id, ticker, relationship, role or "", role_category or ""])
    return "edge:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def company_node(company: dict[str, Any]) -> dict[str, Any]:
    ticker = company["ticker"]
    return {"data": {"id": company_cyto_id(ticker), "type": "Company", "ticker": ticker, "company_name": company.get("company_name"), "universe": company.get("universe"), "sector": company.get("sector"), "industry": company.get("industry"), "description_short": company.get("description_short"), "label": ticker}}


def person_node(person: dict[str, Any]) -> dict[str, Any]:
    pid = person["person_id"]
    name = person.get("person_name") or pid
    return {"data": {"id": person_cyto_id(pid), "type": "Person", "person_id": pid, "person_name": person.get("person_name"), "label": name}}


def relationship_edge(person: dict[str, Any], company: dict[str, Any], rel: dict[str, Any], relationship_type: str) -> dict[str, Any]:
    pid = person["person_id"]
    ticker = company["ticker"]
    role = rel.get("role")
    role_category = rel.get("role_category")
    return {"data": {"id": stable_edge_id(pid, ticker, relationship_type, role, role_category), "source": person_cyto_id(pid), "target": company_cyto_id(ticker), "relationship": relationship_type, "role": role, "role_category": role_category, "extraction_time": rel.get("extraction_time")}}


def search_result(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    if kind == "Company":
        return {"id": company_cyto_id(item["ticker"]), "type": "Company", "label": item["ticker"], "ticker": item["ticker"], "company_name": item.get("company_name")}
    return {"id": person_cyto_id(item["person_id"]), "type": "Person", "label": item.get("person_name") or item["person_id"], "person_id": item["person_id"], "person_name": item.get("person_name")}
