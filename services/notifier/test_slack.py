"""Tests for the Slack adapter. No network and no real tokens: the Web API client
and the Bolt app are small fakes that record what they were asked to do."""
import json
import queue
import re

import pytest

from services.notifier.adapters import slack
from services.notifier.adapters.slack import AdapterError, SlackAdapter, blocks, esc, fallback_text

ALLOWED_TYPES = {"header", "section", "context", "actions", "divider"}
PARTS = ["What happened", "Who is involved", "How the money moved", "Why it looks like a mule ring",
         "What to check next"]


def make_card(**over):
    card = {
        "ring_id": "R-0042",
        "version": "3",
        "shape": "scatter_gather",
        "shape_label": "scatter-gather",
        "n_accounts": 7,
        "xgb_score": 0.912,
        "gnn_score": 0.874,
        "total_usd": 48250.0,
        "facts": {"key_account": "A-17", "passed_on_pct": 96},
        "key_facts": [("Key account", "A-17"), ("Share passed on", "96%"),
                      ("Began passing on after", "2.5 h"), ("Collector", "A-99")],
        "summary": [(p, "Text for %s." % p.lower()) for p in PARTS],
        "reasons": ["fan-out then fan-in within 6 hours", "amounts just under $10,000"],
        "source": "granite",
        "source_label": "Granite (fact-checked)",
        "check_note": "All 12 figures matched the facts pack.",
        "written_at": "2026-10-18 13:58 UTC",
        "dashboard_url": "http://localhost:8501",
        "status": {"decision": None, "decided_by": "", "decided_at": "", "note": "", "label": ""},
        "markers": [],
        "buttons_enabled": True,
    }
    card.update(over)
    return card


def all_texts(bs):
    """Every text string anywhere in the blocks."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("text"), str):
                out.append(x["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(bs)
    return out


def joined(bs):
    return "\n".join(all_texts(bs))


# ---------------------------------------------------------------- rendering

def test_structure_is_valid_block_kit():
    bs = blocks(make_card())
    assert bs[0]["type"] == "header"
    assert bs[0]["text"]["text"] == "Ring R-0042 v3 · scatter-gather"
    for b in bs:
        assert b["type"] in ALLOWED_TYPES
        if b["type"] == "section":
            assert "text" in b or "fields" in b
            assert len(b.get("fields", [])) <= 10
        if b["type"] == "context":
            assert 1 <= len(b["elements"]) <= 10
    json.dumps(bs)  # serialisable


def test_header_scores_and_facts_render():
    text = joined(blocks(make_card()))
    assert "0.912" in text and "0.874" in text
    assert "$48,250" in text
    assert "*Accounts*\n7" in text
    assert "*Key account*\nA-17" in text
    for p in PARTS:
        assert "*%s*" % p in text


def test_missing_gnn_score_is_a_dash_not_zero():
    text = joined(blocks(make_card(gnn_score=None)))
    assert "*GNN score*\n–" in text
    assert "*GNN score*\n0" not in text


def test_long_summary_truncated_under_limit():
    summary = [(p, "x" * 10000) for p in PARTS]
    bs = blocks(make_card(summary=summary))
    for t in all_texts(bs):
        assert len(t) < 3000
    assert any(t.endswith("…") for t in all_texts(bs))


def test_many_fields_split_across_sections():
    facts = [("Fact %d" % i, str(i)) for i in range(15)]
    bs = blocks(make_card(key_facts=facts))
    for b in bs:
        assert len(b.get("fields", [])) <= 10
    assert "*Fact 14*" in joined(bs)


def test_buttons_when_enabled():
    bs = blocks(make_card())
    actions = [b for b in bs if b["type"] == "actions"]
    assert len(actions) == 1
    els = actions[0]["elements"]
    assert [e["action_id"] for e in els] == ["decide_freeze", "decide_dismiss", "decide_more_info"]
    for e in els:
        assert json.loads(e["value"]) == {"ring_id": "R-0042", "version": "3"}
    styles = {e["action_id"]: e.get("style") for e in els}
    assert styles == {"decide_freeze": "danger", "decide_dismiss": None, "decide_more_info": "primary"}


def test_decision_line_replaces_buttons():
    status = {"decision": "freeze", "decided_by": "jane.doe", "decided_at": "2026-10-18 14:02 UTC",
              "note": "confirmed with branch", "label": "Freeze"}
    bs = blocks(make_card(status=status, buttons_enabled=False))
    assert not [b for b in bs if b["type"] == "actions"]
    text = joined(bs)
    assert "*Decision:* Freeze recorded by jane.doe at 2026-10-18 14:02 UTC" in text
    assert "confirmed with branch" in text


def test_markers_appear():
    text = joined(blocks(make_card(markers=["updated to v3", "Granite summary arrived"])))
    assert "updated to v3" in text and "Granite summary arrived" in text


def test_footer_has_dashboard_link_source_and_check_note():
    text = joined(blocks(make_card()))
    assert "<http://localhost:8501|Open in dashboard>" in text
    assert "Granite (fact-checked)" in text
    assert "All 12 figures matched" in text
    assert "2026-10-18 13:58 UTC" in text


def test_block_count_under_50_on_worst_case():
    card = make_card(key_facts=[("Fact %d" % i, "v" * 500) for i in range(8)],
                     summary=[(p, "y" * 10000) for p in PARTS],
                     reasons=["reason %d" % i for i in range(10)],
                     markers=["updated to v3", "Granite summary arrived", "re-opened: new money"])
    assert len(blocks(card)) < 50


def test_block_count_capped_even_with_absurd_input():
    card = make_card(key_facts=[("F%d" % i, "v") for i in range(500)],
                     summary=[("P%d" % i, "t") for i in range(100)])
    assert len(blocks(card)) < 50


def test_esc():
    assert esc("a & b <c> d") == "a &amp; b &lt;c&gt; d"
    assert esc(None) == ""
    text = joined(blocks(make_card(summary=[("What happened", "<!channel> & <http://evil|x>")])))
    assert "<!channel>" not in text
    assert "&lt;!channel&gt; &amp;" in text


def test_fallback_text():
    t = fallback_text(make_card(gnn_score=None))
    assert t.startswith("Ring R-0042 v3 · scatter-gather")
    assert "GNN –" in t
    assert "Text for what happened." in t


def test_blocks_survive_an_empty_card():
    bs = blocks({})
    assert bs[0]["type"] == "header"


# ---------------------------------------------------------------- outbound

class FakeClient:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, **kw):
        self.calls.append(("chat_postMessage", kw))
        return {"ok": True, "channel": kw["channel"], "ts": "1700000000.000100"}

    def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return {"ok": True}

    def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))
        return {"ok": True}

    def views_open(self, **kw):
        self.calls.append(("views_open", kw))
        return {"ok": True}


@pytest.fixture
def no_env(monkeypatch):
    for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        monkeypatch.delenv(k, raising=False)


def test_post_update_upload(no_env):
    client = FakeClient()
    a = SlackAdapter(channel="C123", client=client)
    card = make_card()
    ref = a.post(card)
    assert ref == "C123:1700000000.000100"
    name, kw = client.calls[0]
    assert name == "chat_postMessage" and kw["channel"] == "C123"
    assert kw["blocks"] == blocks(card) and kw["text"] == fallback_text(card)

    a.update(ref, make_card(version="4"))
    name, kw = client.calls[1]
    assert name == "chat_update" and kw["channel"] == "C123" and kw["ts"] == "1700000000.000100"
    assert kw["blocks"][0]["text"]["text"].startswith("Ring R-0042 v4")

    a.upload(ref, "R-0042-v3.md", "# packet")
    name, kw = client.calls[2]
    assert name == "files_upload_v2"
    assert kw["thread_ts"] == "1700000000.000100" and kw["content"] == "# packet"
    assert kw["channel"] == "C123" and kw["filename"] == "R-0042-v3.md"


def test_bad_ref_raises(no_env):
    a = SlackAdapter(channel="C123", client=FakeClient())
    with pytest.raises(AdapterError):
        a.update("nonsense", make_card())


def test_slack_api_error_becomes_adapter_error(no_env):
    from slack_sdk.errors import SlackApiError

    class Failing(FakeClient):
        def chat_postMessage(self, **kw):
            raise SlackApiError("boom", {"ok": False, "error": "channel_not_found"})

    a = SlackAdapter(channel="C123", client=Failing())
    with pytest.raises(AdapterError, match="channel_not_found"):
        a.post(make_card())


TOKENISH = re.compile(r"xox[abp]-[A-Za-z0-9-]{6,}|xapp-[A-Za-z0-9-]{6,}")


def test_missing_channel_raises_without_leaking_tokens(no_env, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1234567890-secretsecret")
    with pytest.raises(AdapterError) as e:
        SlackAdapter()
    assert "SLACK_CHANNEL" in str(e.value)
    assert not TOKENISH.search(str(e.value))


def test_missing_bot_token_raises(no_env, monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL", "C123")
    with pytest.raises(AdapterError) as e:
        SlackAdapter()
    assert "SLACK_BOT_TOKEN" in str(e.value)
    assert not TOKENISH.search(str(e.value))


def test_missing_app_token_raises_on_start(no_env, monkeypatch):
    a = SlackAdapter(bot_token="xoxb-1234567890-secretsecret", channel="C123",
                     client=FakeClient(), app=FakeApp())
    with pytest.raises(AdapterError) as e:
        a.start(queue.Queue())
    assert "SLACK_APP_TOKEN" in str(e.value)
    assert not TOKENISH.search(str(e.value))


def test_built_client_has_rate_limit_retry(no_env):
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
    a = SlackAdapter(bot_token="xoxb-test", channel="C123")
    handlers = [h for h in a.client.retry_handlers if isinstance(h, RateLimitErrorRetryHandler)]
    assert handlers and handlers[0].max_retry_count == 3


# ---------------------------------------------------------------- clicks

class FakeApp:
    def __init__(self):
        self.actions = {}
        self.views = {}

    def action(self, action_id):
        def deco(fn):
            self.actions[action_id] = fn
            return fn
        return deco

    def view(self, callback_id):
        def deco(fn):
            self.views[callback_id] = fn
            return fn
        return deco


class FakeHandler:
    instances = []

    def __init__(self, app, app_token):
        self.app = app
        self.connected = False
        self.closed = False
        FakeHandler.instances.append(self)

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True


class Ack:
    def __init__(self):
        self.count = 0

    def __call__(self, *a, **kw):
        self.count += 1


@pytest.fixture
def started(no_env, monkeypatch):
    import slack_bolt.adapter.socket_mode as sm
    monkeypatch.setattr(sm, "SocketModeHandler", FakeHandler)
    app, client, clicks = FakeApp(), FakeClient(), queue.Queue()
    a = SlackAdapter(bot_token="xoxb-test", app_token="xapp-test", channel="C123",
                     client=client, app=app)
    a.start(clicks)
    return a, app, client, clicks


def button_body(action_id, user=None):
    return {"user": user or {"id": "U1", "username": "jane", "name": "jane.d"},
            "trigger_id": "T-1",
            "actions": [{"action_id": action_id,
                         "value": json.dumps({"ring_id": "R-0042", "version": "3"})}]}


def test_start_registers_handlers_and_connects(started):
    a, app, _, _ = started
    assert set(app.actions) == {"decide_freeze", "decide_dismiss", "decide_more_info"}
    assert set(app.views) == {"decide_note"}
    assert a.handler.connected
    a.stop()
    assert FakeHandler.instances[-1].closed
    assert a.handler is None


def test_freeze_click_lands_on_queue(started):
    _, app, _, clicks = started
    ack = Ack()
    app.actions["decide_freeze"](ack=ack, body=button_body("decide_freeze"))
    assert ack.count == 1
    assert clicks.get_nowait() == {"ring_id": "R-0042", "version": "3", "decision": "freeze",
                                   "user_id": "U1", "user_name": "jane", "note": ""}


def test_dismiss_click_falls_back_to_name(started):
    _, app, _, clicks = started
    app.actions["decide_dismiss"](ack=Ack(), body=button_body("decide_dismiss", {"id": "U2", "name": "bob"}))
    c = clicks.get_nowait()
    assert c["decision"] == "dismiss" and c["user_id"] == "U2" and c["user_name"] == "bob"


def test_more_info_opens_modal_then_submission_lands(started):
    _, app, client, clicks = started
    ack = Ack()
    app.actions["decide_more_info"](ack=ack, body=button_body("decide_more_info"), client=client)
    assert ack.count == 1
    assert clicks.empty()
    name, kw = client.calls[-1]
    assert name == "views_open" and kw["trigger_id"] == "T-1"
    view = kw["view"]
    assert view["callback_id"] == "decide_note"
    meta = json.loads(view["private_metadata"])
    assert meta == {"ring_id": "R-0042", "version": "3"}
    assert any(b.get("element", {}).get("type") == "plain_text_input" for b in view["blocks"])

    submission = {"user": {"id": "U1", "username": "jane"},
                  "view": {"private_metadata": view["private_metadata"],
                           "state": {"values": {"note_block": {"note_input": {
                               "type": "plain_text_input", "value": " need KYC docs "}}}}}}
    ack2 = Ack()
    app.views["decide_note"](ack=ack2, body=submission)
    assert ack2.count == 1
    assert clicks.get_nowait() == {"ring_id": "R-0042", "version": "3", "decision": "more_info",
                                   "user_id": "U1", "user_name": "jane", "note": "need KYC docs"}


def test_click_without_ring_is_acked_and_ignored(started):
    _, app, _, clicks = started
    ack = Ack()
    body = {"user": {"id": "U1"}, "actions": [{"value": "not json"}]}
    app.actions["decide_freeze"](ack=ack, body=body)
    assert ack.count == 1 and clicks.empty()


def test_module_imports_without_slack_libraries():
    """blocks() must not need slack_sdk / slack_bolt: run it in a subprocess
    where both are made unimportable."""
    import subprocess
    import sys
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('slack_sdk', 'slack_bolt'):\n"
        "            raise ImportError('blocked ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from services.notifier.adapters import slack\n"
        "assert slack.blocks({'ring_id': 'R-1'})[0]['type'] == 'header'\n"
        "print('ok')\n"
    )
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
