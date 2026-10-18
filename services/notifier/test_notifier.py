"""Checks for the notifier: cards, decisions, the loop, suppression and the case packet.

Uses fakeredis and the console adapter; no network, no tokens. Cases come from the
real case writer run on its fake rings, so the card sees exactly what the service sends.

    python -m pytest -q services/notifier
"""
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fakeredis = pytest.importorskip("fakeredis")

from common import bus
from services.casewriter import casewriter
from services.casewriter.factcheck import allowed_numbers, is_close, numbers_in
from services.notifier import adapters, cards, decisions, main, packet, suppression
from services.notifier.adapters.console import ConsoleAdapter
from services.notifier.notifier import GROUP, MESSAGES, Notifier

FAKE_RINGS = Path(__file__).resolve().parent.parent / "casewriter" / "fake_rings.json"
RINGS = json.loads(FAKE_RINGS.read_text())
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def local(dt):
    """UTC moment -> the case writer's written_at (naive local time)."""
    return dt.astimezone().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def ring_fields(i):
    return casewriter.ring_to_stream_fields(RINGS[i])


def real_case(i, **over):
    case = casewriter.write_case(ring_fields(i), False)
    case.update({k: str(v) for k, v in over.items()})
    return case


def make_ring(ring_id, version, accounts, amount=1000.0):
    """A chain a0 -> a1 -> ... with one transfer per hop."""
    edges = [{"txn_id": "%s-%d-%d" % (ring_id, version, i), "from": a, "to": b, "amount_usd": amount,
              "ts": "2022-09-08T%02d:00:00" % (10 + i), "score": 0.9}
             for i, (a, b) in enumerate(zip(accounts, accounts[1:]))]
    return {"ring_id": ring_id, "version": version, "shape": "stack", "accounts": accounts,
            "edges": edges, "xgb_score": 0.9, "gnn_score": "", "reasons": ["fast pass-through"]}


def push(r, ring, written):
    """Put a ring on `rings` and its case on `cases`, as the upstream services would."""
    fields = casewriter.ring_to_stream_fields(ring)
    r.xadd(bus.RINGS, fields)
    case = casewriter.write_case(fields, False)
    case["written_at"] = local(written)
    r.xadd(bus.CASES, case)
    return case


def notifier_for(r, hours=24, adapter=None):
    out = io.StringIO()
    adapter = adapter or ConsoleAdapter(out=out, lines=[], user="tester")
    n = Notifier(r, adapter, hours=hours, block_ms=None, dashboard="http://dash")
    n.start()
    return n, adapter.out


def pending(r):
    return r.xpending(bus.CASES, GROUP)["pending"]


# --- cards ------------------------------------------------------------------------

def test_card_from_real_case_has_five_parts_in_order():
    card = cards.build(real_case(0))
    assert [p[0] for p in card["parts"]] == list(cards.PART_ORDER)
    assert card["title"] == "Ring R-0001 · Scatter-gather · 9 accounts"
    fields = dict(card["fields"])
    assert fields["Possible victim"] == "10_8000EBD30" and fields["Total moved"] == "$29,350"
    assert fields["Transfer model"] == "0.93" and fields["Graph model"] == "0.88"
    assert [b["decision"] for b in card["buttons"]] == ["freeze", "dismiss", "more_info"]


def test_source_label_for_granite_template_and_failed_granite():
    granite = cards.build(real_case(0, source="granite", check_note="passed fact check"))
    template = cards.build(real_case(0))
    failed = cards.build(real_case(0, check_note="granite error: timed out"))
    assert granite["footer"][0] == "Written by Granite, passed the fact check"
    assert template["footer"][0].startswith("Template summary (Granite not used")
    assert "Granite failed" in failed["footer"][0]
    assert "Check: granite error: timed out" in failed["footer"]


def test_missing_gnn_score_is_a_dash_and_missing_facts_still_gives_a_card():
    case = real_case(2)
    facts = json.loads(case["facts"])
    facts["gnn_score"] = None
    case["facts"] = json.dumps(facts)
    assert dict(cards.build(case)["fields"])["Graph model"] == cards.DASH

    bare = dict(real_case(2))
    del bare["facts"]
    card = cards.build(bare)
    assert card["title"] == "Ring R-0003" and card["fields"] == [] and len(card["parts"]) == 5


def test_malformed_case_is_rejected():
    for bad in ({}, {"ring_id": "R-1", "summary": "not json"}, {"ring_id": "R-1", "summary": "{}"}):
        with pytest.raises(ValueError):
            cards.parse(bad)


# --- decisions --------------------------------------------------------------------

def test_decision_matches_the_page_writer_on_the_shared_five_fields():
    from services.page import decisions as page_decisions
    now = datetime(2022, 9, 8, 14, 2, 11, 123000, tzinfo=timezone.utc)
    ours = decisions.build("R-0042", 3, "freeze", " checked ", "console:aryan", "console", now=now)
    theirs = page_decisions.build("R-0042", 3, "freeze", " checked ", now=now)
    shared = ("ring_id", "version", "decision", "note", "decided_at")
    assert {k: ours[k] for k in shared} == {k: theirs[k] for k in shared}
    assert ours["decided_at"] == "2022-09-08T14:02:11.123+00:00"
    assert ours["decided_by"] == "console:aryan" and ours["channel"] == "console"


def test_invalid_decision_channel_or_note_rejected():
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", 1, "approve")
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", 1, "freeze", channel="email")
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", 1, "freeze", note="x" * 1001)
    with pytest.raises(decisions.DecisionError):
        decisions.build("", 1, "freeze")


def test_latest_decision_per_ring(r):
    decisions.write(r, "R-1", 1, "more_info", now=T0)
    decisions.write(r, "R-2", 1, "dismiss", now=T0)
    decisions.write(r, "R-1", 1, "freeze", now=T0 + timedelta(minutes=1))
    latest = decisions.latest(r)
    assert latest["R-1"]["decision"] == "freeze" and latest["R-2"]["decision"] == "dismiss"
    assert [d["decision"] for d in decisions.history(r, "R-1")] == ["more_info", "freeze"]


# --- the loop ---------------------------------------------------------------------

def test_three_fake_rings_through_case_writer_give_three_console_cards(r, monkeypatch):
    """--push-fake, then the case writer, then `python -m services.notifier.main --console --once`."""
    monkeypatch.setenv("NOTIFIER_ADAPTER", "console")
    casewriter.push_fake(str(FAKE_RINGS), r=r)
    casewriter.run_redis(False, r=r, max_loops=1)
    out = io.StringIO()
    assert main.main(["--console", "--once"], r=r, out=out, lines=[]) == 0
    text = out.getvalue()
    for title in ("Ring R-0001 · Scatter-gather", "Ring R-0002 · Fan-out", "Ring R-0003 · Cycle"):
        assert text.count(title) == 1
    assert "posted 3" in text and pending(r) == 0
    assert r.hlen(MESSAGES) == 3


def test_higher_version_updates_and_granite_flip_updates(r):
    r.xadd(bus.CASES, real_case(0))
    n, out = notifier_for(r)
    n.run(until_idle=True)
    assert n.counts["posted"] == 1

    r.xadd(bus.CASES, real_case(0, version=2))
    n.run(until_idle=True)
    assert (n.counts["posted"], n.counts["updated"]) == (1, 1)
    assert "updated to v2" in out.getvalue()

    r.xadd(bus.CASES, real_case(0, version=2, source="granite", check_note="passed fact check"))
    n.run(until_idle=True)
    assert (n.counts["posted"], n.counts["updated"]) == (1, 2)
    assert "Granite summary arrived" in out.getvalue()
    assert json.loads(r.hget(MESSAGES, "R-0001"))["source"] == "granite"


def test_restart_reposts_nothing(r):
    for i in range(3):
        r.xadd(bus.CASES, real_case(i))
    first, _ = notifier_for(r)
    first.run(until_idle=True)
    assert first.counts["posted"] == 3

    for i in range(3):   # the same cases again, e.g. replayed after a crash
        r.xadd(bus.CASES, real_case(i))
    second, out = notifier_for(r)
    assert len(second.records) == 3
    second.run(until_idle=True)
    assert second.counts["posted"] == 0 and second.counts["skipped"] == 3
    assert "Ring R-000" not in out.getvalue()


def test_coalescing_keeps_the_newest_case_per_ring(r):
    r.xadd(bus.CASES, real_case(0))
    r.xadd(bus.CASES, real_case(0, version=2))
    r.xadd(bus.CASES, real_case(0, version=1, source="granite"))
    n, out = notifier_for(r)
    n.run(until_idle=True)
    assert n.counts["posted"] == 1 and n.counts["updated"] == 0
    assert json.loads(r.hget(MESSAGES, "R-0001"))["version"] == 2
    assert pending(r) == 0


def test_malformed_case_is_logged_and_acked(r, capsys):
    r.xadd(bus.CASES, {"ring_id": "R-9", "summary": "not json"})
    r.xadd(bus.CASES, real_case(1))
    n, _ = notifier_for(r)
    n.run(until_idle=True)
    assert n.counts["malformed"] == 1 and n.counts["posted"] == 1
    assert pending(r) == 0
    assert "skipped malformed case" in capsys.readouterr().err


class FlakyAdapter(ConsoleAdapter):
    """Raises like an unreachable chat service until `down` is cleared."""
    down = True

    def post(self, card):
        if self.down:
            raise ConnectionError("chat service unreachable")
        return super().post(card)


def test_unreachable_adapter_leaves_cases_pending_then_sends_them(r):
    for i in range(3):
        r.xadd(bus.CASES, real_case(i))
    adapter = FlakyAdapter(out=io.StringIO(), lines=[], user="tester")
    n, _ = notifier_for(r, adapter=adapter)
    n.run(until_idle=True)
    assert n.counts["posted"] == 0 and pending(r) == 3

    adapter.down = False
    n.run(until_idle=True)
    assert n.counts["posted"] == 3 and pending(r) == 0


def test_click_writes_one_decision_and_a_second_click_is_ignored(r):
    from services.page import state
    from services.page.models import PageState
    for i in range(3):
        r.xadd(bus.CASES, real_case(i))
    n, out = notifier_for(r)
    n.run(until_idle=True)
    adapter = n.adapter

    result = adapter.handle_line("freeze R-0001 confirmed with the bank")
    assert result["ok"] and r.xlen(bus.DECISIONS) == 1
    msg = r.xrange(bus.DECISIONS)[0][1]
    assert (msg["ring_id"], msg["version"], msg["decision"], msg["channel"], msg["decided_by"]) == \
        ("R-0001", "1", "freeze", "console", "console:tester")
    assert result["card"]["decision"]["text"].startswith("Freeze recorded by @tester")
    assert result["card"]["buttons"] == []
    assert "(update to message" in out.getvalue()

    again = adapter.handle_line("dismiss R-0001")
    assert not again["ok"] and "already has a decision" in again["message"]
    assert r.xlen(bus.DECISIONS) == 1

    snap = PageState()
    state.read_decisions(r, snap)
    assert snap.decisions["R-0001"].decision == "freeze"


def test_unknown_adapter_fails_clearly(r, monkeypatch, capsys):
    monkeypatch.setenv("NOTIFIER_ADAPTER", "slack")
    assert main.main(["--once"], r=r, out=io.StringIO(), lines=[]) == 2
    assert "only console" in capsys.readouterr().err
    with pytest.raises(ValueError):
        adapters.make("teams")


def test_no_chat_library_is_imported():
    for path in Path(__file__).resolve().parent.rglob("*.py"):
        if path.name == "test_notifier.py":
            continue
        assert not re.search(r"^\s*(import|from)\s+slack", path.read_text(), re.M), path


# --- suppression ------------------------------------------------------------------

ACCTS = ["1_A1", "2_B2", "3_C3", "4_D4", "5_E5"]


def test_dismissed_ring_is_suppressed_inside_the_window_and_posted_after(r, capsys):
    push(r, make_ring("R-1", 1, ACCTS), T0)
    n, out = notifier_for(r)
    n.run(until_idle=True)
    decisions.write(r, "R-1", 1, "dismiss", decided_by="console:jane", channel="console", now=T0)

    push(r, make_ring("R-1", 2, ACCTS, amount=1100.0), T0 + timedelta(hours=1))
    n.run(until_idle=True)
    assert n.counts["posted"] == 1 and n.counts["suppressed"] == 1 and pending(r) == 0
    assert "suppressed R-1 v2: dismissed by @jane" in capsys.readouterr().out
    assert "Dismissed: new versions are not re-posted for 24 h" in out.getvalue()

    push(r, make_ring("R-1", 3, ACCTS, amount=1150.0), T0 + timedelta(hours=25))
    n.run(until_idle=True)
    assert n.counts["posted"] == 2
    assert "posted again: dismissed more than 24 h ago" in out.getvalue()


def test_material_growth_reopens_a_dismissed_ring(r):
    push(r, make_ring("R-1", 1, ACCTS), T0)
    n, out = notifier_for(r)
    n.run(until_idle=True)
    decisions.write(r, "R-1", 1, "dismiss", decided_by="console:jane", now=T0)

    push(r, make_ring("R-1", 2, ACCTS + ["6_F6", "7_G7", "8_H8"]), T0 + timedelta(hours=1))
    n.run(until_idle=True)
    assert n.counts["posted"] == 2 and n.counts["suppressed"] == 0
    assert "re-opened: ring grew from 5 to 8 accounts" in out.getvalue()


def test_ring_sharing_accounts_with_a_dismissed_ring_gets_the_related_note(r):
    push(r, make_ring("R-1", 1, ACCTS[:4]), T0)
    n, out = notifier_for(r)
    n.run(until_idle=True)
    decisions.write(r, "R-1", 1, "dismiss", decided_by="console:jane", now=T0)

    push(r, make_ring("R-2", 1, ACCTS[:3] + ["9_Z9"]), T0 + timedelta(hours=1))
    n.run(until_idle=True)
    assert n.counts["posted"] == 2 and n.counts["suppressed"] == 0
    assert "related ring R-1 dismissed by @jane" in out.getvalue()
    assert "(3 of 4 accounts shared)" in out.getvalue()


def test_freeze_never_suppresses(r):
    push(r, make_ring("R-1", 1, ACCTS), T0)
    n, out = notifier_for(r)
    n.run(until_idle=True)
    decisions.write(r, "R-1", 1, "freeze", decided_by="console:jane", now=T0)
    push(r, make_ring("R-1", 2, ACCTS, amount=1100.0), T0 + timedelta(hours=1))
    n.run(until_idle=True)
    assert n.counts["suppressed"] == 0 and n.counts["updated"] == 1
    assert "updated to v2" in out.getvalue()


def test_suppression_survives_a_restart(r):
    push(r, make_ring("R-1", 1, ACCTS), T0)
    first, _ = notifier_for(r)
    first.run(until_idle=True)
    decisions.write(r, "R-1", 1, "dismiss", decided_by="console:jane", now=T0)

    push(r, make_ring("R-1", 2, ACCTS, amount=1100.0), T0 + timedelta(hours=2))
    second, _ = notifier_for(r)
    second.run(until_idle=True)
    assert second.counts["suppressed"] == 1 and second.counts["posted"] == 0


def test_suppress_hours_from_env(monkeypatch):
    monkeypatch.setenv("SUPPRESS_HOURS", "6")
    assert suppression.suppress_hours() == 6
    monkeypatch.setenv("SUPPRESS_HOURS", "soon")
    assert suppression.suppress_hours() == 24


# --- case packet ------------------------------------------------------------------

def stray_numbers(text, case, ring):
    """Numbers in the packet that are not in the facts pack. Allowed besides the facts:
    txn_ids, timestamps (numbers_in drops dates), the ring id, version, row cap and the
    edges' own amounts and scores (the transfers table)."""
    facts = json.loads(case["facts"])
    _accounts, edges = packet.ring_parts(ring)
    for e in sorted(edges, key=lambda e: -len(e["txn_id"])):
        text = re.sub(r"\b%s\b" % re.escape(e["txn_id"]), " ", text)
    text = re.sub(r"\b[0-9a-f]{64}\b", " ", text)
    allowed = allowed_numbers(facts) | {float(case["version"]), float(packet.MAX_ROWS), 256.0}
    allowed |= {e["amount_usd"] for e in edges} | {e["score"] for e in edges if e["score"] is not None}
    return [n for n in numbers_in(text) if not any(is_close(n, v) for v in allowed)]


@pytest.mark.parametrize("i", [0, 1, 2])
def test_packet_from_each_fake_case(i, tmp_path):
    case, ring = real_case(i), ring_fields(i)
    text = packet.render(case, [], ring, data_dir=tmp_path)
    for label in cards.PART_LABELS.values():
        assert "**%s.**" % label in text
    for edge in RINGS[i]["edges"]:
        txn_id = edge["txn_id"] if isinstance(edge, dict) else edge[0]
        assert "| %s |" % txn_id in text
    assert stray_numbers(text, case, ring) == []
    assert "not an accusation" in text and "No decision recorded yet." in text


def test_packet_caps_transfers_and_handles_no_edges(tmp_path):
    accounts = ["1_A", "2_B", "3_C"]
    edges = [{"txn_id": "t%03d" % k, "from": accounts[k % 2], "to": accounts[2], "amount_usd": 100.0 + k,
              "ts": "2022-09-08T10:%02d:%02d" % (k // 60, k % 60), "score": 0.9} for k in range(250)]
    ring = {"ring_id": "R-7", "version": "1", "shape": "fan-in", "accounts": json.dumps(accounts),
            "edges": json.dumps(edges), "xgb_score": "0.9", "gnn_score": "", "reasons": "[]"}
    case = casewriter.write_case(ring, False)
    text = packet.render(case, [], ring, data_dir=tmp_path)
    assert "Showing the first 200 of 250 transfers" in text
    assert len(re.findall(r"^\| 2022-09-08 ", text, re.M)) == 200

    bare = packet.render(case, [], None, data_dir=tmp_path)
    assert "No transfer detail" in bare
    assert "| 2_B | possible victim |" in bare or "only the accounts named in the facts" in bare


def test_packet_decision_log_is_in_time_order(tmp_path):
    later = decisions.build("R-0001", 1, "freeze", "bank confirmed", "console:jane", "console",
                            now=T0 + timedelta(minutes=5))
    earlier = decisions.build("R-0001", 1, "more_info", "ask for statements", "console:aryan", "console", now=T0)
    text = packet.render(real_case(0), [later, earlier], ring_fields(0), data_dir=tmp_path)
    log = text.split("## Decision log")[1]
    assert log.index("Needs more info") < log.index("Freeze recorded")
    assert "console:aryan" in log and "bank confirmed" in log


def test_packet_lineage_uses_the_manifest_hash_when_present(tmp_path):
    digest = "b19d39f5" + "0" * 56
    (tmp_path / "cleaning_manifest.json").write_text(json.dumps({
        "inputs": {"HI-Small_accounts.csv": {"sha256": "7" * 64}, "HI-Small_Trans.csv": {"sha256": digest}}}))
    assert digest in packet.lineage(tmp_path)
    assert "no `cleaning_manifest.json`" in packet.lineage(tmp_path / "missing")


def test_packet_command_prints_one_ring(r, tmp_path, monkeypatch):
    monkeypatch.setattr(packet, "DATA_DIR", tmp_path)
    r.xadd(bus.RINGS, ring_fields(2))
    r.xadd(bus.CASES, real_case(2))
    decisions.write(r, "R-0003", 2, "dismiss", "loop between own accounts", "console:jane", now=T0)
    out = io.StringIO()
    assert main.main(["--packet", "r-0003"], r=r, out=out) == 0
    text = out.getvalue()
    assert text.startswith("# Case packet: ring R-0003, version 2")
    assert "| 6300131 |" in text and "loop between own accounts" in text
    assert main.main(["--packet", "R-9999"], r=r, out=io.StringIO()) == 1
