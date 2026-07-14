from scripts.analyze_company_projection import Relationship, analyze, build_profiles, build_projection, concentration, threshold_simulation, topk_simulation, components


def rel(t, p, name=None, role="Director"):
    return Relationship(t, f"{t} Inc", p, name or f"Person {p}", role, "BOARD_OF", "")


def test_single_company_person_creates_no_edge():
    people, _, _ = build_profiles([rel("A", "p1")])
    assert build_projection(people) == {}


def test_two_company_person_creates_one_ordered_edge():
    people, _, _ = build_profiles([rel("B", "p1"), rel("A", "p1")])
    assert build_projection(people) == {("A", "B"): ["p1"]}


def test_three_company_person_creates_three_edges():
    people, _, _ = build_profiles([rel("C", "p1"), rel("A", "p1"), rel("B", "p1")])
    assert sorted(build_projection(people)) == [("A", "B"), ("A", "C"), ("B", "C")]


def test_duplicate_roles_same_company_do_not_double_count_company():
    people, _, _ = build_profiles([rel("A", "p1", role="CEO"), rel("A", "p1", role="Chair"), rel("B", "p1")])
    edges = build_projection(people)
    assert edges == {("A", "B"): ["p1"]}


def test_two_people_same_pair_count_two():
    result = analyze([rel("A", "p1"), rel("B", "p1"), rel("A", "p2"), rel("B", "p2")])
    assert result["summary"]["projection"]["maximum_shared_person_count"] == 2
    assert result["top_edges"][0]["shared_person_count"] == 2


def test_deterministic_ranking_under_ties():
    result = analyze([rel("B", "p1"), rel("A", "p1"), rel("C", "p2"), rel("A", "p2")])
    assert [(r["source_ticker"], r["target_ticker"]) for r in result["top_edges"]] == [("A", "B"), ("A", "C")]


def test_threshold_simulation_correctness():
    people, companies, _ = build_profiles([rel("A", "p1"), rel("B", "p1"), rel("A", "p2"), rel("B", "p2"), rel("B", "p3"), rel("C", "p3")])
    rows = threshold_simulation(build_projection(people), companies, thresholds=(2,))
    assert rows[0]["retained_edge_count"] == 1
    assert rows[0]["companies_with_zero_retained_edges"] == 1


def test_topk_union_semantics_correctness():
    people, companies, _ = build_profiles([rel("A", "p1"), rel("B", "p1"), rel("A", "p2"), rel("C", "p2"), rel("A", "p3"), rel("D", "p3"), rel("B", "p4"), rel("D", "p4")])
    rows = topk_simulation(build_projection(people), companies, ks=(1,))
    assert rows[0]["retained_unique_edge_count"] == 3  # A-B by A/B, A-C by C, A-D by D tie union


def test_connected_components_correctness():
    comps = components(["A", "B", "C", "D"], [("A", "B"), ("C", "D")])
    assert [sorted(c) for c in comps] == [["A", "B"], ["C", "D"]]


def test_high_degree_contribution_calculation_correctness():
    people, _, _ = build_profiles([rel("A", "p1"), rel("B", "p1"), rel("C", "p1"), rel("A", "p2")])
    c = concentration(people)
    assert c["total_person_pair_contributions_before_deduplication"] == 3
    assert c["top_1_person_percentage"] == 100.0
