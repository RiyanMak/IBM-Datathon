"""Regression checks for selection, graph scope and truthful presentation."""
from dataclasses import replace

from services.page import state, workspace as ws
from services.page.models import CaseView, Edge, RingView


def ring(edges=None, accounts=None):
    edges = edges if edges is not None else [Edge("t1", "A", "B", 100, "2022-09-08T10:00:00", .9)]
    return RingView("R-1", 1, "other", len(accounts or []), accounts or [], edges, .9, None, [], "", "", 0)


def test_transfer_identity_survives_order_and_amount_changes():
    a = Edge("same", "A", "B", 10, "2022-01-01", .8)
    b = Edge("same", "B", "C", 20, "2022-01-02", .7)
    first = ws.transfers(ring([a, b]))
    second = ws.transfers(ring([b, replace(a, amount_usd=11)]))
    assert [key for key, _ in first] == [key for key, _ in second]
    assert len(set(key for key, _ in ws.transfers(ring([a, a, b])))) == 3


def test_canonical_ids_never_collapse_when_shortened_labels_match():
    a, b = "same-prefix-111111-same-end", "same-prefix-222222-same-end"
    r = ring([Edge("t", a, b, 10, "", None)])
    data = ws.graph_data(r)
    assert {n["id"] for n in data["nodes"]} == {a, b}
    assert len({n["label"] for n in data["nodes"]}) == 2
    assert len(ws.related_transfers(r, "account:" + a)) == 1


def test_unknown_amount_is_not_zero_and_partial_volume_is_labelled():
    missing = Edge("x", "A", "B", None, "", None)
    assert ws.volume([missing]) == "–"
    assert ws.volume([missing, replace(missing, amount_usd=0)]) == "≈ $0 · partial"
    assert ws.native_amount(None, "EUR") == "Not supplied"
    assert ws.native_amount(1000.25, "EUR") == "1,000.25 EUR"


def test_native_currency_is_optional_and_legacy_edges_still_parse():
    legacy = state.parse_edge(["t", "A", "B", 10, "2022", .5])
    enriched = state.parse_edge({"txn_id": "t", "from": "A", "to": "B", "amt_paid": "9.5", "ccy_paid": "EUR"})
    assert legacy.amt_paid is None
    assert (enriched.amt_paid, enriched.ccy_paid) == (9.5, "EUR")


def test_large_graph_is_bounded_and_selected_transfer_remains_visible():
    edges = [Edge(str(i), "A"+str(i), "A"+str(i+1), i, str(i), .5) for i in range(120)]
    r = ring(edges)
    key = next(k for k, e in ws.transfers(r) if e.txn_id == "0")
    data = ws.graph_data(r, key)
    assert len(data["nodes"]) <= ws.MAX_NODES
    assert len(data["edges"]) <= ws.MAX_TRANSFERS
    assert data["limited"] and "120 transfers" in data["scope"]
    assert key in [e["key"] for e in data["edges"]]
    assert len(ws.related_transfers(r, "overview")) == 120


def test_existing_positions_survive_new_version():
    r = ring()
    old = ws.graph_data(r)
    updated = replace(r, version=2, edges=r.edges + [Edge("t2", "B", "C", 40, "2023", .8)])
    new = ws.graph_data(updated, positions=old["positions"])
    for a, pos in old["positions"].items():
        assert new["positions"][a] == pos


def test_self_loop_disconnected_and_empty_graphs():
    r = ring([Edge("t", "A", "A", 5, "", None)], ["A", "B"])
    data = ws.graph_data(r)
    assert len(data["nodes"]) == 2
    assert data["edges"][0]["src"] == data["edges"][0]["dst"]
    assert "do not establish" in ws.flow_description(r)
    assert ws.graph_data(ring([]))["nodes"] == []


def test_queue_holds_rank_and_requires_explicit_new_rows():
    assert ws.stable_queue(None, ["A", "B"]) == (["A", "B"], 0)
    assert ws.stable_queue(["A", "B"], ["C", "B", "A"]) == (["A", "B"], 1)
    assert ws.stable_queue(["A", "B"], ["C", "B", "A"], apply=True) == (["C", "B", "A"], 0)


def test_granite_label_requires_an_actual_passing_check():
    c = CaseView("R", 1, [], "granite", "FAKE upgrade: template text relabelled, no Granite call", "", None, 0)
    assert not c.fact_checked and ws.provenance(c) == "Simulated upgrade · template text"
    assert not replace(c, check_note="granite not used").fact_checked
    assert replace(c, check_note="passed fact check").fact_checked
    assert "fact check passed" in ws.provenance(replace(c, check_note="passed fact check"))
