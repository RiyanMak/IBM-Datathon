"""Checks for the reviewer bot's core: cards, decisions, suppression and the loop.

Uses fakeredis and the console adapter, so no network, Slack or Granite is needed.
Cases are real ones, made by the case writer's own write_case() from its fake rings.

    python -m pytest -q services/notifier
"""
import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fakeredis = pytest.importorskip("fakeredis")

from common import bus
from services.casewriter.casewriter import ring_to_stream_fields, write_case
from services.casewriter.template import REQUIRED_KEYS
from services.notifier import cards, decisions, suppression
from services.notifier.adapters.base import Adapter, AdapterError
from services.notifier.adapters.console import ConsoleAdapter, render
from services.notifier.notifier import MESSAGES, Notifier, adapter_kind, decided_by
from services.page import decisions as page_decisions

FAKE_RINGS = Path(__file__).resolve().parents[1] / "casewriter" / "fake_rings.json"
with open(FAKE_RINGS) as fh:
    RINGS = json.load(fh)

T0 = datetime(2026, 10, 18, 14, 2, 11, 123000, tzinfo=timezone.utc)


# --- helpers --------------------------------------------------------------------

@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def no_packet(monkeypatch):
    """Tests run as if packet.py were absent, unless a test installs a fake one."""
    monkeypatch.setitem(sys.modules, "services.notifier.packet", None)


class Recorder(Adapter):
    """Remembers every call, like the console adapter but without printing."""
    kind = "console"

    def __init__(self):
        self.posts, self.updates, self.uploads = [], [], []
        self.fail = False

    def post(self, card):
        if self.fail:
            raise AdapterError("network down")
        self.posts.append(card)
        return "rec:%d" % len(self.posts)

    def update(self, message_ref, card):
        if self.fail:
            raise AdapterError("network down")
        self.updates.append((message_ref, card))

    def upload(self, message_ref, filename, text):
        self.uploads.append((message_ref, filename, text))


def fields(i, **over):
    ring = dict(RINGS[i])
    ring.update(over)
    return ring_to_stream_fields(ring)


def make_ring(ring_id, version, accounts, amount=1000.0):
    """A chain ring through `accounts`, as the ring service would send it."""
    edges = [{"txn_id": str(100 + k), "from": a, "to": b, "amount_usd": amount,
              "ts": "2022-09-08T1%d:00:00" % k, "score": 0.9}
             for k, (a, b) in enumerate(zip(accounts, accounts[1:]))]
    return ring_to_stream_fields({
        "ring_id": ring_id, "version": version, "shape": "stack", "n_accounts": len(accounts),
        "accounts": accounts, "edges": edges, "xgb_score": 0.9, "gnn_score": "",
        "reasons": ["fast pass-through"], "first_seen": edges[0]["ts"], "last_seen": edges[-1]["ts"]})


def push(r, ring, source="template", written_at=None):
    """Put the ring on `rings` and its case on `cases`. Returns the case fields."""
    r.xadd(bus.RINGS, ring)
    case = write_case(ring, use_granite=False)
    if source == "granite":
        case["source"], case["check_note"] = "granite", "passed fact check"
    if written_at is not None:
        case["written_at"] = written_at.strftime("%Y-%m-%dT%H:%M:%S")
    r.xadd(bus.CASES, case)
    return case


def notifier(r, adapter=None, hours=24):
    return Notifier(r, adapter or Recorder(), "http://dash", hours, consumer="test")


def drain(n):
    while n.run_once(block_ms=None):
        pass


def dismiss(r, ring_id, version, at, by="console:jane"):
    decisions.write(r, ring_id, version, "dismiss", "looks fine", by, "console", now=at)


# --- cards ------------------------------------------------------------------------

def test_card_from_template_case_has_every_key_and_five_parts():
    case = write_case(fields(0), use_granite=False)
    card = cards.build(case, fields(0), dashboard_url="http://dash")
    assert tuple(card) == cards.CARD_KEYS
    assert [label for label, _ in card["summary"]] == [cards.part_label(k) for k in REQUIRED_KEYS]
    assert all(text for _, text in card["summary"])
    assert card["source"] == "template" and card["source_label"] == "Template"
    assert card["shape_label"] == "Scatter-gather"
    assert card["n_accounts"] == 9 and card["xgb_score"] == 0.93 and card["gnn_score"] == 0.88
    assert card["key_facts"][0] == ("Key account (possible victim)", "10_8000EBD30")
    assert ("Share passed on", "98%") in card["key_facts"]
    assert ("Began passing on after", "0.7 h") in card["key_facts"]
    assert len(card["key_facts"]) <= 8
    assert card["reasons"][0] == "money left 40 minutes after it arrived"
    assert card["status"]["decision"] is None and card["buttons_enabled"] is True


def test_card_granite_label():
    case = write_case(fields(1), use_granite=False)
    case["source"] = "granite"
    assert cards.build(case)["source_label"] == "Granite, fact-checked"


def test_missing_gnn_score_is_a_dash_not_zero():
    ring = fields(2, gnn_score="")
    card = cards.build(write_case(ring, use_granite=False), ring)
    assert card["gnn_score"] is None
    assert cards.score(card["gnn_score"]) == "–"
    assert "GNN –" in render(card)


def test_card_without_facts_still_builds():
    case = {"ring_id": "R-9", "version": "2", "summary": "not json", "source": "template",
            "facts": "{broken"}
    card = cards.build(case)
    assert card["facts"] == {} and card["key_facts"] == []
    assert card["n_accounts"] is None and card["total_usd"] is None
    assert len(card["summary"]) == 5
    assert "R-9 v2" in render(card)
    assert cards.build({})["ring_id"] == ""


def test_helpers_dash_for_none():
    assert [cards.money(None), cards.pct(None), cards.hours(None), cards.score(None)] == ["–"] * 4
    assert cards.money(12345.4) == "$12,345" and cards.pct(98) == "98%"


def test_card_status_disables_buttons():
    case = write_case(fields(0), use_granite=False)
    d = decisions.build("R-0001", "1", "freeze", "", "slack:U1:jane.doe", "slack", now=T0)
    card = cards.build(case, status=d)
    assert card["buttons_enabled"] is False
    assert card["status"]["decision"] == "freeze" and card["status"]["label"] == "Frozen"
    assert "Frozen by jane.doe at 2026-10-18 14:02 UTC" in render(card)


# --- decisions -------------------------------------------------------------------

def test_decision_matches_page_on_shared_fields():
    ours = decisions.build("R-0042", "3", "freeze", "  checked  ", "slack:U0123ABC:jane.doe", "slack", now=T0)
    page = page_decisions.build("R-0042", "3", "freeze", "  checked  ", now=T0)
    shared = ("ring_id", "version", "decision", "note", "decided_at")   # the page adds channel/decided_by of its own
    assert {k: ours[k] for k in shared} == {k: page[k] for k in shared}
    assert ours["decided_at"] == "2026-10-18T14:02:11.123+00:00"
    assert ours["decided_by"] == "slack:U0123ABC:jane.doe" and ours["channel"] == "slack"
    assert all(isinstance(v, str) for v in ours.values())


def test_decision_validation():
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", "1", "arrest", "", "console:a", "console")
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", "1", "freeze", "", "console:a", "email")
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", "1", "freeze", "x" * 1001, "console:a", "console")
    with pytest.raises(decisions.DecisionError):
        decisions.build("R-1", "abc", "freeze", "", "console:a", "console")
    assert decisions.build("R-1", "", "freeze", "", "console:a", "console")["version"] == ""


def test_decision_latest_and_history(r):
    decisions.write(r, "R-1", "1", "more_info", "", "console:a", "console", now=T0)
    decisions.write(r, "R-2", "1", "freeze", "", "console:a", "console", now=T0)
    decisions.write(r, "R-1", "1", "freeze", "", "console:b", "console", now=T0 + timedelta(minutes=1))
    latest = decisions.latest(r)
    assert latest["R-1"]["decision"] == "freeze" and latest["R-2"]["decision"] == "freeze"
    assert [d["decision"] for d in decisions.history(r, "R-1")] == ["more_info", "freeze"]


def test_decided_by_and_adapter_kind():
    assert decided_by("slack", "U0123ABC", "jane.doe") == "slack:U0123ABC:jane.doe"
    assert decided_by("console", "console", "aryan") == "console:aryan"

    class SlackAdapter(Adapter):
        channel = "C0123"   # the Slack adapter's channel ID must not be mistaken for the kind
    assert adapter_kind(SlackAdapter()) == "slack" and adapter_kind(ConsoleAdapter()) == "console"


# --- the loop ---------------------------------------------------------------------

def test_three_cases_three_posts_then_update_for_higher_version(r):
    for i in range(3):
        push(r, fields(i))
    rec = Recorder()
    n = notifier(r, rec)
    drain(n)
    assert [c["ring_id"] for c in rec.posts] == ["R-0001", "R-0002", "R-0003"]
    assert r.hlen(MESSAGES) == 3
    snap = json.loads(r.hget(MESSAGES, "R-0001"))
    assert snap["ref"] == "rec:1" and snap["version"] == "1" and snap["source"] == "template"
    assert r.xpending(bus.CASES, "notifier")["pending"] == 0

    push(r, fields(0, version=2))
    drain(n)
    assert len(rec.posts) == 3
    ref, card = rec.updates[-1]
    assert ref == "rec:1" and card["version"] == "2" and "updated to v2" in card["markers"]


def test_granite_flip_same_version_updates_with_marker(r):
    push(r, fields(0))
    rec = Recorder()
    n = notifier(r, rec)
    drain(n)
    push(r, fields(0), source="granite")
    drain(n)
    assert len(rec.posts) == 1
    card = rec.updates[-1][1]
    assert card["markers"] == ["Granite summary arrived"] and card["source"] == "granite"
    # a late template for the same version does not downgrade the card
    push(r, fields(0))
    drain(n)
    assert len(rec.updates) == 1


def test_batch_coalesces_newest_per_ring(r):
    push(r, fields(0))
    push(r, fields(0, version=2))
    push(r, fields(0, version=2), source="granite")
    rec = Recorder()
    drain(notifier(r, rec))
    assert len(rec.posts) == 1 and rec.posts[0]["version"] == "2" and rec.posts[0]["source"] == "granite"
    assert r.xpending(bus.CASES, "notifier")["pending"] == 0


def test_restart_posts_nothing_again(r):
    for i in range(3):
        push(r, fields(i))
    drain(notifier(r))
    rec = Recorder()
    n2 = notifier(r, rec)
    drain(n2)
    assert rec.posts == [] and set(n2.messages) == {"R-0001", "R-0002", "R-0003"}
    push(r, fields(1, version=2))
    drain(n2)
    assert rec.posts == [] and rec.updates[-1][0] == "rec:2"


def test_malformed_case_acked_and_skipped(r, caplog):
    r.xadd(bus.CASES, {"version": "1", "summary": "{}"})
    push(r, fields(0))
    rec = Recorder()
    drain(notifier(r, rec))
    assert len(rec.posts) == 1
    assert r.xpending(bus.CASES, "notifier")["pending"] == 0
    assert "malformed case" in caplog.text


def test_failed_post_is_not_acked_and_is_retried(r):
    push(r, fields(0))
    rec = Recorder()
    rec.fail = True
    n = notifier(r, rec)
    n.run_once(block_ms=None)
    assert rec.posts == [] and r.xpending(bus.CASES, "notifier")["pending"] == 1
    rec.fail = False
    n.retry_seconds = 0
    n._retry_at = 0
    n.run_once(block_ms=None)
    assert len(rec.posts) == 1 and r.xpending(bus.CASES, "notifier")["pending"] == 0


def test_console_click_writes_decision_and_disables_buttons(r, monkeypatch):
    monkeypatch.setenv("NOTIFIER_USER", "aryan")
    out = io.StringIO()
    console = ConsoleAdapter(out=out, inp=io.StringIO(""))
    push(r, fields(0))
    n = notifier(r, console)
    drain(n)
    assert "R-0001 v1" in out.getvalue() and "[console:1]" in out.getvalue()
    console.clicks = n.clicks
    console.handle_line("freeze R-0001 confirmed with the bank")
    assert n.handle_clicks() == ["written"]
    msgs = r.xrange(bus.DECISIONS)
    assert len(msgs) == 1
    d = msgs[0][1]
    assert d["decision"] == "freeze" and d["version"] == "1" and d["note"] == "confirmed with the bank"
    assert d["decided_by"] == "console:aryan" and d["channel"] == "console"
    assert "(update to console:1)" in out.getvalue()
    assert "Status: Frozen by aryan" in out.getvalue()

    console.handle_line("dismiss R-0001")
    assert n.handle_clicks() == ["ignored"]
    assert len(r.xrange(bus.DECISIONS)) == 1


def test_click_card_after_restart_shows_status(r):
    push(r, fields(0))
    drain(notifier(r))
    rec = Recorder()
    n2 = notifier(r, rec)
    n2.run_once(block_ms=None)
    n2.clicks.put({"ring_id": "R-0001", "version": "", "decision": "more_info",
                   "user_id": "console", "user_name": "aryan", "note": "need KYC"})
    assert n2.handle_clicks() == ["written"]
    ref, card = rec.updates[-1]
    assert ref == "rec:1" and card["buttons_enabled"] is False
    assert card["status"]["decision"] == "more_info" and card["status"]["note"] == "need KYC"


def test_console_handle_line_parsing():
    c = ConsoleAdapter(out=io.StringIO(), inp=io.StringIO(""))
    assert c.handle_line("more_info R-0042 ask the branch")["note"] == "ask the branch"
    assert c.handle_line("hello") is None and c.handle_line("") is None
    c.handle_line("quit")
    assert c.quit_event.is_set()


def test_console_thread_reads_input(r):
    import queue
    q = queue.Queue()
    c = ConsoleAdapter(out=io.StringIO(), inp=io.StringIO("freeze R-1\nquit\n"))
    c.start(q)
    c.thread.join(2)
    assert q.get_nowait()["decision"] == "freeze" and c.quit_event.is_set()


def test_packet_uploaded_when_module_present(r, monkeypatch):
    fake = types.ModuleType("services.notifier.packet")
    seen = {}

    def fake_render(case_fields, ring_fields, decisions_history, manifest=None):
        seen["ring"] = ring_fields
        return "# packet for %s\n" % case_fields["ring_id"]
    fake.render = fake_render
    monkeypatch.setitem(sys.modules, "services.notifier.packet", fake)
    push(r, fields(0))
    rec = Recorder()
    drain(notifier(r, rec))
    assert rec.uploads == [("rec:1", "case_R-0001_v1.md", "# packet for R-0001\n")]
    assert seen["ring"]["ring_id"] == "R-0001" and isinstance(seen["ring"]["accounts"], list)


def test_no_packet_module_no_upload(r):
    push(r, fields(0))
    rec = Recorder()
    drain(notifier(r, rec))
    assert rec.uploads == []


# --- suppression -----------------------------------------------------------------

def test_dismissed_ring_suppressed_within_window(r, caplog):
    ring = make_ring("R-1", 1, ["a", "b", "c", "d", "e"])
    push(r, ring, written_at=T0)
    rec = Recorder()
    n = notifier(r, rec)
    drain(n)
    dismiss(r, "R-1", "1", T0)
    push(r, make_ring("R-1", 2, ["a", "b", "c", "d", "e"]), written_at=T0 + timedelta(hours=1))
    with caplog.at_level("INFO", logger="notifier"):
        drain(n)
    assert len(rec.posts) == 1
    assert r.xpending(bus.CASES, "notifier")["pending"] == 0
    assert "suppressed R-1 v2: dismissed by jane at 2026-10-18 14:02 UTC" in caplog.text
    # the card is marked once, quietly, with the dismissal
    assert len(rec.updates) == 1 and rec.updates[0][1]["status"]["decision"] == "dismiss"
    push(r, make_ring("R-1", 3, ["a", "b", "c", "d", "e"]), written_at=T0 + timedelta(hours=2))
    drain(n)
    assert len(rec.posts) == 1 and len(rec.updates) == 1


def test_dismissed_ring_posted_after_window(r):
    push(r, make_ring("R-1", 1, ["a", "b", "c", "d", "e"]), written_at=T0)
    rec = Recorder()
    n = notifier(r, rec)
    drain(n)
    dismiss(r, "R-1", "1", T0)
    push(r, make_ring("R-1", 2, ["a", "b", "c", "d", "e"]), written_at=T0 + timedelta(hours=25))
    drain(n)
    assert len(rec.posts) == 2
    card = rec.posts[-1]
    assert card["buttons_enabled"] is True and card["markers"][0].startswith("re-opened")
    # the re-opened card can be decided again
    n.clicks.put({"ring_id": "R-1", "version": "2", "decision": "freeze",
                  "user_id": "console", "user_name": "a", "note": ""})
    assert n.handle_clicks() == ["written"]


def test_growth_reopens_within_window(r):
    push(r, make_ring("R-1", 1, ["a", "b", "c", "d", "e"]), written_at=T0)
    rec = Recorder()
    n = notifier(r, rec)
    drain(n)
    dismiss(r, "R-1", "1", T0)
    push(r, make_ring("R-1", 2, list("abcdefgh")), written_at=T0 + timedelta(hours=1))
    drain(n)
    assert len(rec.posts) == 2
    assert rec.posts[-1]["markers"][0] == "re-opened: ring grew from 5 to 8 accounts"


def test_related_ring_note(r):
    push(r, make_ring("R-1", 1, ["a", "b", "c", "d"]), written_at=T0)
    rec = Recorder()
    n = notifier(r, rec)
    drain(n)
    dismiss(r, "R-1", "1", T0, by="slack:U1:jane")
    push(r, make_ring("R-2", 1, ["a", "b", "c", "z"]), written_at=T0 + timedelta(hours=1))
    drain(n)
    assert rec.posts[-1]["ring_id"] == "R-2"
    assert "related ring R-1 dismissed by jane at 2026-10-18 14:02 UTC" in rec.posts[-1]["markers"]


def test_freeze_and_more_info_never_suppress(r):
    for kind in ("freeze", "more_info"):
        rid = "R-%s" % kind
        push(r, make_ring(rid, 1, ["a", "b", "c"]), written_at=T0)
        rec = Recorder()
        n = notifier(r, rec)
        drain(n)
        decisions.write(r, rid, "1", kind, "", "console:jane", "console", now=T0)
        push(r, make_ring(rid, 2, ["a", "b", "c"]), written_at=T0 + timedelta(hours=1))
        drain(n)
        assert rec.updates and rec.updates[-1][1]["version"] == "2"
        assert rec.updates[-1][1]["status"]["decision"] == kind


def test_suppression_survives_restart(r):
    push(r, make_ring("R-1", 1, ["a", "b", "c", "d", "e"]), written_at=T0)
    drain(notifier(r))
    dismiss(r, "R-1", "1", T0)
    push(r, make_ring("R-1", 2, ["a", "b", "c", "d", "e"]), written_at=T0 + timedelta(hours=1))
    rec = Recorder()
    drain(notifier(r, rec))
    assert rec.posts == [] and r.xpending(bus.CASES, "notifier")["pending"] == 0


def test_suppression_pure_functions():
    d = {"decision": "dismiss", "decided_at": "2026-10-18T14:02:11.123+00:00", "decided_by": "console:jane"}
    assert suppression.is_suppressed(d, "2026-10-18T15:02:00", 24)
    assert not suppression.is_suppressed(d, "2026-10-19T15:02:00", 24)
    assert not suppression.is_suppressed(dict(d, decision="freeze"), "2026-10-18T15:02:00", 24)
    assert not suppression.is_suppressed(dict(d, decided_at="garbage"), "2026-10-18T15:02:00", 24)
    assert not suppression.is_suppressed(None, "2026-10-18T15:02:00", 24)
    assert suppression.material_change({"n_accounts": 5}, {"n_accounts": 6}) is None
    assert suppression.material_change({"total_usd": 1000}, {"total_usd": 1500}) == \
        "re-opened: total grew from $1,000 to $1,500"
    assert suppression.material_change({}, {"n_accounts": 50}) is None
    assert suppression.related_dismissed(["a", "b"], {"R-1": (["a", "b"], d)}, ring_id="R-1") is None
    assert suppression.related_dismissed(["a", "b", "c"], {"R-1": (["a"], d)}) is None
    action, markers = suppression.decide({"ring_id": "R-1", "version": "1"}, None, {}, {}, {}, 24)
    assert (action, markers) == ("post", [])


def test_real_packet_attaches_when_present(r, monkeypatch):
    monkeypatch.delitem(sys.modules, "services.notifier.packet")
    pytest.importorskip("services.notifier.packet")
    push(r, fields(0))
    rec = Recorder()
    drain(notifier(r, rec))
    assert len(rec.uploads) == 1 and rec.uploads[0][1] == "case_R-0001_v1.md"
    assert "R-0001" in rec.uploads[0][2]


# --- main ---------------------------------------------------------------------------

def test_main_once_posts_with_console(r, monkeypatch, capsys):
    from services.notifier import main as main_mod
    monkeypatch.setattr(main_mod.bus, "connect", lambda **kw: r)
    monkeypatch.setattr(main_mod, "load_env_file", lambda *a, **kw: None)
    for i in range(3):
        push(r, fields(i))
    assert main_mod.main(["--console", "--once"]) == 0
    out = capsys.readouterr().out
    assert out.count("[console:") == 3
    assert r.hlen(MESSAGES) == 3


def test_main_falls_back_to_console_without_slack_tokens(monkeypatch, capsys):
    from services.notifier import main as main_mod
    for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL", "TEAMS_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    assert isinstance(main_mod.make_adapter("slack"), ConsoleAdapter)
    assert isinstance(main_mod.make_adapter("teams"), ConsoleAdapter)
    assert "using the console adapter" in capsys.readouterr().err


def test_main_packet_prints_latest(r, monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "services.notifier.packet")
    pytest.importorskip("services.notifier.packet")
    from services.notifier import main as main_mod
    monkeypatch.setattr(main_mod.bus, "connect", lambda **kw: r)
    monkeypatch.setattr(main_mod, "load_env_file", lambda *a, **kw: None)
    push(r, fields(2))
    assert main_mod.main(["--packet", "R-0003"]) == 0
    assert "R-0003" in capsys.readouterr().out
    assert main_mod.main(["--packet", "R-9999"]) == 1
