#!/usr/bin/env python3
"""Analyze the canonical company-to-company projection from SQLite relationships."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

COLUMNS = ("ticker", "company_name", "person_id", "person_name", "role", "role_category", "extraction_time")


@dataclass(frozen=True)
class Relationship:
    ticker: str
    company_name: str
    person_id: str
    person_name: str
    role: str
    role_category: str
    extraction_time: str = ""


@dataclass
class PersonProfile:
    person_id: str
    person_name: str = ""
    companies: dict[str, str] = field(default_factory=dict)
    roles_by_company: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    categories_by_company: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def pct(part: int | float, total: int | float) -> float:
    return round((float(part) / float(total) * 100.0), 6) if total else 0.0


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 6)


def read_relationships(db_path: Path) -> list[Relationship]:
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT ticker, company_name, person_id, person_name, role, role_category, extraction_time "
            "FROM relationships"
        ).fetchall()
    return [Relationship(*(clean(row[col]) for col in COLUMNS)) for row in rows]


def build_profiles(rows: Iterable[Relationship]) -> tuple[dict[str, PersonProfile], dict[str, str], int]:
    people: dict[str, PersonProfile] = {}
    companies: dict[str, str] = {}
    exact = Counter()
    for r in rows:
        exact[(r.ticker, r.company_name, r.person_id, r.person_name, r.role, r.role_category, r.extraction_time)] += 1
        companies.setdefault(r.ticker, r.company_name)
        p = people.setdefault(r.person_id, PersonProfile(person_id=r.person_id, person_name=r.person_name))
        if not p.person_name and r.person_name:
            p.person_name = r.person_name
        p.companies.setdefault(r.ticker, r.company_name)
        if r.role:
            p.roles_by_company[r.ticker].add(r.role)
        if r.role_category:
            p.categories_by_company[r.ticker].add(r.role_category)
    duplicate_rows = sum(count - 1 for count in exact.values() if count > 1)
    return people, companies, duplicate_rows


def build_projection(people: dict[str, PersonProfile]) -> dict[tuple[str, str], list[str]]:
    edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    for person_id in sorted(people):
        tickers = sorted(people[person_id].companies)
        for source, target in combinations(tickers, 2):
            edges[(source, target)].append(person_id)
    return {edge: sorted(ids) for edge, ids in sorted(edges.items())}


def components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[set[str]]:
    adj: dict[str, set[str]] = {n: set() for n in sorted(nodes)}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen: set[str] = set()
    out: list[set[str]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        comp: set[str] = set()
        q = deque([start]); seen.add(start)
        while q:
            n = q.popleft(); comp.add(n)
            for nxt in sorted(adj[n]):
                if nxt not in seen:
                    seen.add(nxt); q.append(nxt)
        out.append(comp)
    return sorted(out, key=lambda c: (-len(c), sorted(c)[0] if c else ""))


def edge_count_for_component(comp: set[str], edge_keys: Iterable[tuple[str, str]]) -> int:
    return sum(1 for a, b in edge_keys if a in comp and b in comp)


def summarize_distribution(edges: dict[tuple[str, str], list[str]]) -> list[dict[str, Any]]:
    total = len(edges)
    counts = Counter(len(v) for v in edges.values())
    specs = [("=1", 1, "eq"), ("=2", 2, "eq"), ("=3", 3, "eq"), ("=4", 4, "eq"), ("=5", 5, "eq"), (">=6", 6, "ge"), (">=2", 2, "ge"), (">=3", 3, "ge"), (">=5", 5, "ge"), (">=10", 10, "ge")]
    rows = []
    for label, n, op in specs:
        c = counts[n] if op == "eq" else sum(v for k, v in counts.items() if k >= n)
        rows.append({"shared_person_count": label, "edge_count": c, "edge_percentage": pct(c, total)})
    return rows


def degree_bucket_rows(degrees: list[int]) -> list[dict[str, Any]]:
    specs = [("=1", lambda d: d == 1), ("=2", lambda d: d == 2), ("=3", lambda d: d == 3), ("=4", lambda d: d == 4), ("=5", lambda d: d == 5), ("6-10", lambda d: 6 <= d <= 10), ("11-20", lambda d: 11 <= d <= 20), (">20", lambda d: d > 20)]
    return [{"person_company_degree": label, "person_count": sum(1 for d in degrees if pred(d)), "person_percentage": pct(sum(1 for d in degrees if pred(d)), len(degrees))} for label, pred in specs]


def threshold_simulation(edges: dict[tuple[str, str], list[str]], companies: dict[str, str], thresholds=(1, 2, 3, 5, 10)) -> list[dict[str, Any]]:
    total = len(edges)
    rows = []
    for t in thresholds:
        kept = sorted(e for e, ids in edges.items() if len(ids) >= t)
        nodes = sorted({x for e in kept for x in e})
        comps = components(nodes, kept) if nodes else []
        rows.append({"threshold": t, "retained_edge_count": len(kept), "retained_edge_percentage": pct(len(kept), total), "companies_with_at_least_one_retained_edge": len(nodes), "companies_with_zero_retained_edges": len(companies) - len(nodes), "connected_component_count": len(comps), "largest_connected_component_size": len(comps[0]) if comps else 0})
    return rows


def topk_simulation(edges: dict[tuple[str, str], list[str]], companies: dict[str, str], ks=(3, 5, 10)) -> list[dict[str, Any]]:
    incident: dict[str, list[tuple[int, str, tuple[str, str]]]] = defaultdict(list)
    for e, ids in edges.items():
        a, b = e; w = len(ids)
        incident[a].append((-w, b, e)); incident[b].append((-w, a, e))
    total = len(edges); rows = []
    for k in ks:
        kept: set[tuple[str, str]] = set()
        for ticker in sorted(companies):
            for _, _, e in sorted(incident.get(ticker, []))[:k]:
                kept.add(e)
        kept_edges = sorted(kept)
        nodes = sorted({x for e in kept_edges for x in e})
        comps = components(nodes, kept_edges) if nodes else []
        rows.append({"top_k": k, "retained_unique_edge_count": len(kept_edges), "retained_edge_percentage": pct(len(kept_edges), total), "companies_with_at_least_one_retained_edge": len(nodes), "companies_with_zero_retained_edges": len(companies) - len(nodes), "connected_component_count": len(comps), "largest_connected_component_size": len(comps[0]) if comps else 0})
    return rows


def concentration(people: dict[str, PersonProfile]) -> dict[str, Any]:
    contribs = sorted(((p.person_id, len(p.companies), len(p.companies) * (len(p.companies) - 1) // 2) for p in people.values()), key=lambda x: (-x[2], x[0]))
    total = sum(c for _, _, c in contribs)
    return {"total_person_pair_contributions_before_deduplication": total, "top_1_person_percentage": pct(sum(c for *_, c in contribs[:1]), total), "top_5_people_percentage": pct(sum(c for *_, c in contribs[:5]), total), "top_10_people_percentage": pct(sum(c for *_, c in contribs[:10]), total), "top_50_people_percentage": pct(sum(c for *_, c in contribs[:50]), total), "degree_at_least_5_percentage": pct(sum(c for _, d, c in contribs if d >= 5), total), "degree_at_least_10_percentage": pct(sum(c for _, d, c in contribs if d >= 10), total), "degree_at_least_20_percentage": pct(sum(c for _, d, c in contribs if d >= 20), total)}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)


def analyze(rows: list[Relationship], top_people=50, top_companies=50, top_edges=100) -> dict[str, Any]:
    people, companies, duplicates = build_profiles(rows)
    edges = build_projection(people)
    shared_counts = [len(v) for v in edges.values()]
    connected_companies = sorted({t for e in edges for t in e})
    person_degrees = {pid: len(p.companies) for pid, p in people.items()}
    company_neighbors: dict[str, set[str]] = {t: set() for t in companies}
    company_strength = Counter({t: 0 for t in companies})
    for (a, b), ids in edges.items():
        company_neighbors[a].add(b); company_neighbors[b].add(a)
        company_strength[a] += len(ids); company_strength[b] += len(ids)
    company_degrees = [len(company_neighbors[t]) for t in sorted(companies)]
    comps = components(connected_companies, edges.keys()) if connected_companies else []
    component_rows = []
    for i, comp in enumerate(comps[:20], 1):
        tickers = sorted(comp); preview = tickers if len(tickers) <= 100 else tickers[:100]
        component_rows.append({"component_rank": i, "company_count": len(comp), "edge_count": edge_count_for_component(comp, edges.keys()), "tickers": json.dumps(preview, sort_keys=True), "ticker_list_truncated": len(tickers) > 100})
    top_people_rows = [{"person_id": pid, "person_name": people[pid].person_name, "person_company_degree": person_degrees[pid], "company_tickers": ";".join(sorted(people[pid].companies))} for pid in sorted(people, key=lambda p: (-person_degrees[p], p))[:top_people]]
    company_distribution = [{"ticker": t, "company_name": companies[t], "company_projected_degree": len(company_neighbors[t]), "total_shared_person_connections": company_strength[t]} for t in sorted(companies)]
    top_company_rows = sorted(company_distribution, key=lambda r: (-r["company_projected_degree"], r["ticker"]))[:top_companies]
    top_edge_rows = []
    for (a, b), ids in sorted(edges.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][1]))[:top_edges]:
        evidence = []
        for pid in ids:
            p = people[pid]
            evidence.append({"person_id": pid, "person_name": p.person_name, "source_roles": sorted(p.roles_by_company.get(a, set())), "source_role_categories": sorted(p.categories_by_company.get(a, set())), "target_roles": sorted(p.roles_by_company.get(b, set())), "target_role_categories": sorted(p.categories_by_company.get(b, set()))})
        top_edge_rows.append({"source_ticker": a, "source_company_name": companies.get(a, ""), "target_ticker": b, "target_company_name": companies.get(b, ""), "shared_person_count": len(ids), "shared_people_evidence": json.dumps(evidence, sort_keys=True, separators=(",", ":"))})
    summary = {"canonical_input": {"total_relationship_rows": len(rows), "unique_companies": len(companies), "unique_people": len(people), "duplicate_exact_relationship_rows": duplicates, "people_with_exactly_1_distinct_company": sum(1 for d in person_degrees.values() if d == 1), "people_with_more_than_1_distinct_company": sum(1 for d in person_degrees.values() if d > 1)}, "projection": {"total_unique_unordered_company_edges": len(edges), "companies_with_cross_company_connections": len(connected_companies), "companies_without_cross_company_connections": len(companies) - len(connected_companies), "minimum_shared_person_count": min(shared_counts) if shared_counts else 0, "maximum_shared_person_count": max(shared_counts) if shared_counts else 0, "mean_shared_person_count": round(mean(shared_counts), 6) if shared_counts else 0.0, "median_shared_person_count": median(shared_counts) if shared_counts else 0.0}, "person_company_degree": {"maximum": max(person_degrees.values()) if person_degrees else 0, "mean": round(mean(person_degrees.values()), 6) if person_degrees else 0.0, "median": median(person_degrees.values()) if person_degrees else 0.0, "p90": percentile(list(person_degrees.values()), 0.90), "p95": percentile(list(person_degrees.values()), 0.95), "p99": percentile(list(person_degrees.values()), 0.99)}, "company_projected_degree": {"minimum": min(company_degrees) if company_degrees else 0, "maximum": max(company_degrees) if company_degrees else 0, "mean": round(mean(company_degrees), 6) if company_degrees else 0.0, "median": median(company_degrees) if company_degrees else 0.0, "p90": percentile(company_degrees, 0.90), "p95": percentile(company_degrees, 0.95), "p99": percentile(company_degrees, 0.99)}, "connected_components": {"total_connected_components_companies_in_projection": len(comps), "largest_connected_component_company_count": len(comps[0]) if comps else 0, "largest_connected_component_percentage_of_all_companies": pct(len(comps[0]) if comps else 0, len(companies)), "singleton_companies_with_no_cross_company_edge": len(companies) - len(connected_companies)}}
    return {"summary": summary, "edges": edges, "companies": companies, "shared_distribution": summarize_distribution(edges), "person_distribution": degree_bucket_rows(list(person_degrees.values())), "top_people": top_people_rows, "company_distribution": company_distribution, "top_companies": top_company_rows, "top_edges": top_edge_rows, "components": component_rows, "thresholds": threshold_simulation(edges, companies), "topk": topk_simulation(edges, companies), "concentration": concentration(people)}


def write_outputs(result: dict[str, Any], output_dir: Path, export_complete_projection: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(result["summary"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "projection_concentration.json").write_text(json.dumps(result["concentration"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(output_dir / "shared_person_distribution.csv", result["shared_distribution"], ["shared_person_count", "edge_count", "edge_percentage"])
    write_csv(output_dir / "person_degree_distribution.csv", result["person_distribution"], ["person_company_degree", "person_count", "person_percentage"])
    write_csv(output_dir / "top_people_by_company_degree.csv", result["top_people"], ["person_id", "person_name", "person_company_degree", "company_tickers"])
    write_csv(output_dir / "company_degree_distribution.csv", result["company_distribution"], ["ticker", "company_name", "company_projected_degree", "total_shared_person_connections"])
    write_csv(output_dir / "top_companies_by_projected_degree.csv", result["top_companies"], ["ticker", "company_name", "company_projected_degree", "total_shared_person_connections"])
    write_csv(output_dir / "top_company_edges.csv", result["top_edges"], ["source_ticker", "source_company_name", "target_ticker", "target_company_name", "shared_person_count", "shared_people_evidence"])
    write_csv(output_dir / "connected_components.csv", result["components"], ["component_rank", "company_count", "edge_count", "tickers", "ticker_list_truncated"])
    write_csv(output_dir / "threshold_simulation.csv", result["thresholds"], ["threshold", "retained_edge_count", "retained_edge_percentage", "companies_with_at_least_one_retained_edge", "companies_with_zero_retained_edges", "connected_component_count", "largest_connected_component_size"])
    write_csv(output_dir / "topk_simulation.csv", result["topk"], ["top_k", "retained_unique_edge_count", "retained_edge_percentage", "companies_with_at_least_one_retained_edge", "companies_with_zero_retained_edges", "connected_component_count", "largest_connected_component_size"])
    if export_complete_projection:
        rows = [{"source_ticker": a, "target_ticker": b, "shared_person_count": len(ids)} for (a, b), ids in sorted(result["edges"].items())]
        write_csv(output_dir / "complete_company_projection.csv", rows, ["source_ticker", "target_ticker", "shared_person_count"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/constellation.db", type=Path)
    parser.add_argument("--output-dir", default="data/analysis/company_projection", type=Path)
    parser.add_argument("--top-people", default=50, type=int)
    parser.add_argument("--top-companies", default=50, type=int)
    parser.add_argument("--top-edges", default=100, type=int)
    parser.add_argument("--export-complete-projection", action="store_true")
    args = parser.parse_args()
    result = analyze(read_relationships(args.db), args.top_people, args.top_companies, args.top_edges)
    write_outputs(result, args.output_dir, args.export_complete_projection)
    print(json.dumps(result["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
