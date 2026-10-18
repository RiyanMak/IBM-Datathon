"""Tests for the Flask fallback of the investigator page. Uses fakeredis; skipped without flask."""
import json

import pytest

fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("flask")

from common import bus
from services.page.flask_app import create_app
from services.page.models import decision_label

FROZEN = decision_label("freeze")   # the page's own wording, so a label change never breaks these tests


def ring_msg(ring_id="R-1", version=1, **over):
    msg = {
        "ring_id": ring_id, "version": str(version), "shape": "scatter-gather", "n_accounts": "3",
        "accounts": json.dumps(["1_A", "2_B", "3_C"]),
        "edges": json.dumps([
            {"txn_id": "t1", "from": "1_A", "to": "2_B", "amount_usd": 100.0, "ts": "2022-09-08T10:00:00", "score": 0.9},
            {"txn_id": "t2", "from": "2_B", "to": "3_C", "amount_usd": 95.0, "ts": "2022-09-08T10:30:00", "score": 0.8},
        ]),
        "xgb_score": "0.85", "gnn_score": "", "reasons": json.dumps(["paid by ACH", "new payee"]),
        "first_seen": "2022-09-08T10:00:00", "last_seen": "2022-09-08T10:30:00",
    }
    msg.update(over)
    return msg


def case_msg(ring_id="R-1", version=1, note="granite not used"):
    return {"ring_id": ring_id, "version": str(version), "source": "template", "check_note": note,
            "written_at": "2026-10-17T20:00:00",
            "summary": json.dumps({"summary": "Three accounts moved $195.", "what_happened": "A paid B, B paid C."}),
            "facts": json.dumps({"ring_id": ring_id, "shape": "scatter-gather", "n_accounts": 3,
                                 "key_account": "2_B", "received_usd": 100, "passed_on_pct": 95})}


@pytest.fixture
def client():
    r = fakeredis.FakeRedis(decode_responses=True)
    app = create_app(lambda: r)
    app.config["TESTING"] = True
    return app.test_client(), r


def test_page_and_health(client):
    c, _ = client
    assert c.get("/healthz").data == b"ok"
    html = c.get("/").data.decode()
    assert "Mule Ring Hunter" in html and "/api/state" in html and "viz-standalone" in html


def test_state_lists_rings_with_decisions_and_cases(client):
    c, r = client
    r.xadd(bus.RINGS, ring_msg())
    r.xadd(bus.RINGS, ring_msg("R-2", shape="fan-out", xgb_score="0.6"))
    r.xadd(bus.CASES, case_msg())
    r.xadd(bus.DECISIONS, {"ring_id": "R-2", "version": "1", "decision": "freeze", "note": "", "decided_at": "2026-10-17T20:01:00+00:00"})
    s = c.get("/api/state").get_json()
    assert s["redis_ok"] and s["errors"] == [] and s["tiles"]
    by_id = {x["ring_id"]: x for x in s["rings"]}
    assert set(by_id) == {"R-1", "R-2"}
    assert by_id["R-1"]["shape_label"] == "Scatter-gather" and by_id["R-1"]["has_case"] and by_id["R-1"]["decision"] is None
    assert by_id["R-2"]["decision"]["label"] == FROZEN and by_id["R-2"]["gnn_score"] == "–"
    assert s["open_rings"] == 1


def test_ring_detail_has_picture_reasons_facts_and_case(client):
    c, r = client
    r.xadd(bus.RINGS, ring_msg())
    r.xadd(bus.CASES, case_msg())
    d = c.get("/api/ring/R-1").get_json()
    assert d["dot"].startswith("digraph") and "2_B" in d["dot"]
    assert d["reasons"] == ["paid by ACH", "new payee"]
    assert any("95" in v for _, v in d["facts"])
    assert d["case"]["source_label"] == "Template" and d["case"]["parts"][0]["text"].startswith("Three accounts")
    assert d["history"][0]["version"] == 1
    assert c.get("/api/ring/R-9").status_code == 404


def test_ring_detail_asks_for_granite_and_reads_the_case_hash(client):
    from services.page import flask_app
    flask_app._granite_throttle.store.clear()   # the throttle is per process; earlier tests used R-1
    c, r = client
    r.xadd(bus.RINGS, ring_msg())
    r.hset("case:R-1", mapping=case_msg(note="granite on request"))   # hash only: outside the window
    d = c.get("/api/ring/R-1").get_json()
    assert d["case"]["granite_writing"] and d["case"]["parts"]
    requests = r.xrange(bus.GRANITE_REQUESTS)
    assert len(requests) == 1 and requests[0][1]["requested_by"] == "flask"
    c.get("/api/ring/R-1")
    assert r.xlen(bus.GRANITE_REQUESTS) == 1   # throttled on refresh


def test_decide_writes_the_stream_and_rejects_bad_input(client):
    c, r = client
    r.xadd(bus.RINGS, ring_msg())
    ok = c.post("/api/decide", json={"ring_id": "R-1", "version": 1, "decision": "freeze", "note": "mule M"})
    assert ok.status_code == 200 and ok.get_json()["label"] == FROZEN
    saved = r.xrange(bus.DECISIONS, "-", "+")[0][1]
    assert saved["ring_id"] == "R-1" and saved["decision"] == "freeze" and saved["note"] == "mule M" and saved["version"] == "1"
    bad = c.post("/api/decide", json={"ring_id": "R-1", "version": 1, "decision": "shrug"})
    assert bad.status_code == 400 and "decision must be one of" in bad.get_json()["error"]
    assert c.get("/api/state").get_json()["rings"][0]["decision"]["label"] == FROZEN


def test_page_survives_redis_being_down():
    import redis

    class Down:
        def __getattr__(self, name):
            raise redis.exceptions.ConnectionError("down")

    c = create_app(lambda: Down()).test_client()
    s = c.get("/api/state").get_json()
    assert s["redis_ok"] is False and s["rings"] == []
    assert c.post("/api/decide", json={"ring_id": "R-1", "version": 1, "decision": "freeze"}).status_code == 503


def test_case_packet_downloads_as_markdown(client):
    c, r = client
    r.xadd(bus.RINGS, ring_msg())
    r.xadd(bus.CASES, case_msg())
    r.xadd(bus.DECISIONS, {"ring_id": "R-1", "version": "1", "decision": "freeze", "note": "mule",
                           "decided_at": "2026-10-17T20:01:00+00:00"})
    res = c.get("/api/ring/R-1/packet.md?version=1")
    assert res.status_code == 200
    assert res.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert res.headers["Content-Disposition"] == 'attachment; filename="case_R-1_v1.md"'
    text = res.data.decode()
    assert text.startswith("# Case packet: R-1, version 1")
    assert "Three accounts moved $195." in text and "| t1 |" in text and "Frozen" in text
    assert "/packet.md" in c.get("/").data.decode()


def test_case_packet_without_a_case_is_404_json(client):
    c, r = client
    r.xadd(bus.RINGS, ring_msg())
    for path in ("/api/ring/R-1/packet.md", "/api/ring/R-9/packet.md"):
        res = c.get(path)
        assert res.status_code == 404 and "no case yet" in res.get_json()["error"]
