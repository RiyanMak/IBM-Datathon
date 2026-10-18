"""Checks for the ring picture. Builds rings in memory; never imports services.page.fake."""
import re

from services.page import graph
from services.page.models import Edge, RingView


def make_ring(edges, accounts=None):
    accts = accounts if accounts is not None else sorted({x for e in edges for x in (e.src, e.dst)})
    return RingView(ring_id="R-1", version=1, shape="fan-out", n_accounts=len(accts),
                    accounts=accts, edges=edges, xgb_score=0.9, gnn_score=None, reasons=[],
                    first_seen="", last_seen="", received_ms=0)


def e(src, dst, amount, ts="2022-09-08T10:00:00", txn="t", score=0.9):
    return Edge(txn_id=txn, src=src, dst=dst, amount_usd=amount, ts=ts, score=score)


def nodes(dot):
    return re.findall(r'^  "([^"]+)" \[', dot, re.M)


def arrows(dot):
    return re.findall(r'^  "([^"]+)" -> "([^"]+)" \[label="([^"]+)"', dot, re.M)


def test_money():
    assert graph.money(9800.0) == "$9,800"
    assert graph.money(950.25) == "$950"
    assert graph.money(None) == "$?"


def test_every_account_and_transfer_is_drawn():
    ring = make_ring([e("A", "B", 100), e("B", "C", 90)], accounts=["A", "B", "C", "D"])
    pic = graph.build(ring)
    assert nodes(pic.dot) == ["A", "B", "C", "D"]          # D has no transfer but is in the ring
    assert arrows(pic.dot) == [("A", "B", "$100"), ("B", "C", "$90")]
    assert not pic.truncated and pic.caption == ""


def test_parallel_transfers_merge():
    ring = make_ring([e("A", "B", 100, "2022-09-08T11:00:00"), e("A", "B", 50, "2022-09-08T10:00:00")])
    pic = graph.build(ring)
    assert arrows(pic.dot) == [("A", "B", "$150 x2")]
    tip = re.search(r'tooltip="(A -> B[^"]*)"', pic.dot).group(1)
    assert tip.index("10:00") < tip.index("11:00")          # oldest transfer first


def test_roles_from_facts_are_highlighted_and_written():
    ring = make_ring([e("O", "K", 1000), e("K", "C", 950)])
    pic = graph.build(ring, facts={"key_account": "K", "origin": "O", "collector": "C"})
    assert pic.roles == {"K": "key_account", "O": "origin", "C": "collector"}
    assert "Key account (inferred)" in pic.dot and "Collector" in pic.dot
    assert graph.THEMES["light"]["roles"]["key_account"] in pic.dot


def test_no_facts_means_no_highlight():
    pic = graph.build(make_ring([e("A", "B", 1)]), facts=None)
    assert pic.roles == {} and "possible victim" not in pic.dot


def test_facts_naming_an_unknown_account_are_ignored():
    pic = graph.build(make_ring([e("A", "B", 1)]), facts={"key_account": "Z", "collector": None})
    assert pic.roles == {}


def test_one_account_with_two_roles_keeps_the_first():
    pic = graph.build(make_ring([e("A", "B", 1)]), facts={"key_account": "B", "collector": "B"})
    assert pic.roles == {"B": "key_account"}


def test_big_ring_keeps_largest_transfers():
    edges = [e("A%d" % i, "B%d" % i, float(i)) for i in range(1, 101)]
    pic = graph.build(make_ring(edges), max_edges=10)
    assert pic.truncated and pic.caption == "Showing the 10 largest of 100 transfers."
    assert sorted(int(a[2][1:]) for a in arrows(pic.dot)) == list(range(91, 101))
    assert len(nodes(pic.dot)) == 20                         # untouched accounts dropped


def test_quotes_and_angle_brackets_are_escaped():
    ring = make_ring([e('A"x', "B<y>", 5)])
    pic = graph.build(ring, facts={"key_account": "B<y>"})
    assert '"A\\"x"' in pic.dot
    assert "B&lt;y&gt;" in pic.dot                          # inside the HTML label


def test_missing_amounts_and_empty_ring_draw():
    pic = graph.build(make_ring([e("A", "B", None)]))
    assert arrows(pic.dot) == [("A", "B", "$?")]
    empty = graph.build(make_ring([], accounts=[]))
    assert empty.dot.startswith("digraph") and empty.dot.endswith("}")


def test_dark_theme_uses_dark_ink():
    pic = graph.build(make_ring([e("A", "B", 1)]), theme="dark")
    assert graph.THEMES["dark"]["ink"] in pic.dot and graph.THEMES["light"]["ink"] not in pic.dot


def test_arrow_width_follows_share_of_the_largest():
    assert graph._pen(10_000, 10_000) == 3.0
    assert graph._pen(100, 10_000) == 1.0
    assert graph._pen(1, 10_000) == 1.0
    assert abs(graph._pen(990, 1310) - graph._pen(1310, 1310)) < 0.3   # similar amounts, similar widths
