"""The Slack adapter: turns a neutral card into a Block Kit message and turns
button clicks back into plain click dicts for the notifier core.

Two halves. `blocks()`, `fallback_text()` and `esc()` are pure functions with no
Slack library needed, so the card layout can be tested on any machine. The
`SlackAdapter` class does the talking: post, update in place, attach a file in
the message's thread, and receive clicks over Socket Mode (an outbound websocket,
so no public URL or inbound port is needed).

No business logic lives here. The core decides what goes on the card and what a
click means; this file only renders the card and forwards who clicked what.
Tokens come from the environment and are never printed.
"""
import json
import logging
import os
import queue

try:
    from services.notifier.adapters.base import Adapter, AdapterError
except ImportError:  # core not merged yet; same shape
    class AdapterError(Exception):
        pass

    class Adapter:
        pass

log = logging.getLogger(__name__)

DASH = "–"
ELLIPSIS = "…"

# Slack's own limits (api.slack.com/reference/block-kit). We stay a little under.
MAX_BLOCKS = 50
MAX_FIELDS = 10
MAX_SECTION_TEXT = 2900      # section text limit is 3,000
MAX_FIELD_TEXT = 1900        # each field's limit is 2,000
MAX_HEADER_TEXT = 150
MAX_CONTEXT_ELEMENTS = 10
MAX_FALLBACK = 3000

ACTION_IDS = {
    "freeze": "decide_freeze",
    "dismiss": "decide_dismiss",
    "more_info": "decide_more_info",
}
NOTE_VIEW_ID = "decide_note"
NOTE_BLOCK_ID = "note_block"
NOTE_ACTION_ID = "note_input"

DECISION_WORDS = {"freeze": "Freeze", "dismiss": "Dismiss", "more_info": "Needs more info"}


# ---------------------------------------------------------------- rendering

def esc(text) -> str:
    """Escape the three characters Slack mrkdwn treats as control: &, <, >.
    Applied to every piece of data text so a ring note cannot inject a link or mention."""
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _cut(text: str, limit: int) -> str:
    """Shorten to at most `limit` characters, ending with an ellipsis when cut."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def _score(x) -> str:
    """A model score as 0.000; a dash when absent, never a misleading 0."""
    if x is None:
        return DASH
    try:
        return "%.3f" % float(x)
    except (TypeError, ValueError):
        return DASH


def _usd(x) -> str:
    """A dollar total as $12,345; a dash when absent."""
    if x is None:
        return DASH
    try:
        return "${:,.0f}".format(float(x))
    except (TypeError, ValueError):
        return DASH


def _count(x) -> str:
    """An account count as 1,234; a dash when absent."""
    if x is None:
        return DASH
    try:
        return "{:,}".format(int(x))
    except (TypeError, ValueError):
        return DASH


def _mrkdwn(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _context(texts) -> dict:
    """A small grey line; Slack allows at most 10 elements per context block."""
    return {"type": "context",
            "elements": [_mrkdwn(_cut(t, MAX_FIELD_TEXT)) for t in texts[:MAX_CONTEXT_ELEMENTS]]}


def _field_sections(fields) -> list:
    """Fields split into sections of at most 10, Slack's per-section limit."""
    return [{"type": "section", "fields": fields[i:i + MAX_FIELDS]}
            for i in range(0, len(fields), MAX_FIELDS)]


def title(card: dict) -> str:
    """'Ring R-0042 v3 · scatter-gather', used for the header and the fallback text."""
    shape = card.get("shape_label") or card.get("shape") or ""
    text = "Ring %s v%s" % (card.get("ring_id") or "?", card.get("version") or "?")
    return text + (" · " + shape if shape else "")


def _decision_line(status: dict) -> str:
    """'Decision: Freeze recorded by jane.doe at 2026-10-18 14:02 UTC'."""
    word = status.get("label") or DECISION_WORDS.get(status.get("decision"), str(status.get("decision")))
    line = "*Decision:* %s" % esc(word)
    if status.get("decided_by"):
        line += " recorded by %s" % esc(status["decided_by"])
    if status.get("decided_at"):
        line += " at %s" % esc(status["decided_at"])
    return line


def _buttons(card: dict) -> dict:
    """The three decision buttons; each carries {ring_id, version} so a click on an
    old card still says which version the person saw."""
    value = json.dumps({"ring_id": card.get("ring_id") or "", "version": card.get("version") or ""})

    def button(decision, label, style=None):
        b = {"type": "button", "action_id": ACTION_IDS[decision],
             "text": {"type": "plain_text", "text": label}, "value": value}
        if style:
            b["style"] = style
        return b

    return {"type": "actions", "block_id": "decide",
            "elements": [button("freeze", "Freeze accounts", "danger"),
                         button("dismiss", "Dismiss"),
                         button("more_info", "Needs more info", "primary")]}


def blocks(card: dict) -> list:
    """The card as Block Kit JSON, kept inside Slack's limits: under 50 blocks,
    at most 10 fields per section, every section text under 3,000 characters.
    When space runs short, key facts and summary parts are dropped from the end
    rather than the header, decision line or buttons."""
    card = card or {}
    status = card.get("status") or {}

    head = [{"type": "header",
             "text": {"type": "plain_text", "text": _cut(title(card), MAX_HEADER_TEXT), "emoji": True}}]
    markers = [m for m in (card.get("markers") or []) if m]
    if markers:
        head.append(_context([esc(m) for m in markers]))

    head.append({"type": "section", "fields": [
        _mrkdwn("*Accounts*\n%s" % _count(card.get("n_accounts"))),
        _mrkdwn("*XGBoost score*\n%s" % _score(card.get("xgb_score"))),
        _mrkdwn("*GNN score*\n%s" % _score(card.get("gnn_score"))),
        _mrkdwn("*Total USD*\n%s" % _usd(card.get("total_usd"))),
    ]})

    facts = []
    for pair in (card.get("key_facts") or [])[:20]:
        try:
            label, value = pair
        except (TypeError, ValueError):
            continue
        facts.append(_mrkdwn(_cut("*%s*\n%s" % (esc(label), esc(value)), MAX_FIELD_TEXT)))
    fact_blocks = _field_sections(facts)

    summary_blocks = []
    for pair in card.get("summary") or []:
        try:
            label, text = pair
        except (TypeError, ValueError):
            continue
        if not text:
            continue
        summary_blocks.append({"type": "section", "text": _mrkdwn(
            _cut("*%s*\n%s" % (esc(label), esc(text)), MAX_SECTION_TEXT))})

    tail = []
    reasons = [r for r in (card.get("reasons") or []) if r]
    if reasons:
        tail.append(_context(["*Why flagged:* " + esc("; ".join(str(r) for r in reasons))]))

    footer = []
    if card.get("source_label"):
        footer.append(esc(card["source_label"]))
    if card.get("check_note"):
        footer.append(esc(_cut(str(card["check_note"]), 300)))
    if card.get("written_at"):
        footer.append("written %s" % esc(card["written_at"]))
    if card.get("dashboard_url"):
        # The URL goes inside <...|...>, so escape only what would break the link syntax.
        url = str(card["dashboard_url"]).replace("|", "%7C").replace(">", "%3E").replace("<", "%3C")
        footer.append("<%s|Open in dashboard>" % url)
    if footer:
        tail.append(_context(footer))

    if card.get("buttons_enabled"):
        tail.append(_buttons(card))
    elif status.get("decision"):
        lines = [_decision_line(status)]
        if status.get("note"):
            lines.append("Note: " + esc(status["note"]))
        tail.append(_context(lines))

    # Fit under 50: fixed parts first, then key facts, then summary parts in order.
    room = MAX_BLOCKS - 1 - len(head) - len(tail) - 1  # one spare, one divider
    fact_blocks = fact_blocks[:max(room, 0)]
    room -= len(fact_blocks)
    summary_blocks = summary_blocks[:max(room, 0)]

    out = head + fact_blocks
    if summary_blocks:
        out.append({"type": "divider"})
        out += summary_blocks
    return out + tail


def fallback_text(card: dict) -> str:
    """Plain text for the message's `text` argument: what a phone notification and a
    screen reader show. Title, scores, then the first summary part."""
    card = card or {}
    text = "%s: %s accounts, XGBoost %s, GNN %s, %s" % (
        title(card), _count(card.get("n_accounts")), _score(card.get("xgb_score")),
        _score(card.get("gnn_score")), _usd(card.get("total_usd")))
    for pair in card.get("summary") or []:
        try:
            _, first = pair
        except (TypeError, ValueError):
            continue
        if first:
            text += ". " + str(first)
            break
    return _cut(esc(text), MAX_FALLBACK)


# ---------------------------------------------------------------- the adapter

def _split_ref(message_ref: str):
    """'C123:1700000000.000100' -> ('C123', '1700000000.000100')."""
    channel, sep, ts = (message_ref or "").partition(":")
    if not sep or not channel or not ts:
        raise AdapterError("not a Slack message reference: %r" % message_ref)
    return channel, ts


def _user(body: dict):
    """(user_id, user_name) from an interaction payload."""
    user = (body or {}).get("user") or {}
    return str(user.get("id") or ""), str(user.get("username") or user.get("name") or "")


class SlackAdapter(Adapter):
    """Posts cards to one Slack channel and receives button clicks over Socket Mode.

    `client` and `app` can be injected (a fake in tests); otherwise a WebClient with
    a 429 retry handler and a Bolt App are built from the tokens. Tokens default to
    SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_CHANNEL from the environment.
    """

    kind = "slack"   # the `channel` value the notifier writes on every decision from here

    def __init__(self, bot_token=None, app_token=None, channel=None, client=None, app=None):
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN") or ""
        self.app_token = app_token or os.environ.get("SLACK_APP_TOKEN") or ""
        self.channel = channel or os.environ.get("SLACK_CHANNEL") or ""
        self.app = app
        self.handler = None
        self.clicks = None
        if not self.channel:
            raise AdapterError("SLACK_CHANNEL is not set; put the channel ID in .env")
        if client is None:
            if not self.bot_token:
                raise AdapterError("SLACK_BOT_TOKEN is not set; put the bot token (xoxb-...) in .env")
            client = self._make_client(self.bot_token)
        self.client = client

    @staticmethod
    def _make_client(token):
        """A WebClient that waits and retries up to 3 times when Slack says 429."""
        try:
            from slack_sdk import WebClient
            from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        except ImportError as e:
            raise AdapterError("slack-sdk is not installed (pip install slack-sdk): %s" % e)
        client = WebClient(token=token)
        client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
        return client

    def _call(self, what, fn, **kwargs):
        """Run one Web API call, turning any failure into AdapterError.

        A Slack API error (bad token, unknown channel, 429 after the retries) carries
        Slack's error code. A network failure (Slack unreachable, DNS, timeout) comes
        from urllib as URLError or OSError, not as SlackApiError; it must become an
        AdapterError too, or the notifier would acknowledge the case as malformed and
        never post it. Only AdapterError means "leave it pending and retry".
        """
        try:
            return fn(**kwargs)
        except AdapterError:
            raise
        except Exception as e:
            code = ""
            if type(e).__name__ == "SlackApiError" or _is_slack_error(e):
                resp = getattr(e, "response", None)
                try:
                    code = resp.get("error", "") if resp is not None else ""
                except Exception:
                    pass
            raise AdapterError("Slack %s failed: %s" % (what, code or type(e).__name__)) from e

    # ---- outbound

    def post(self, card: dict) -> str:
        """Post a new card; returns '<channel>:<ts>' for later updates."""
        resp = self._call("chat.postMessage", self.client.chat_postMessage,
                          channel=self.channel, blocks=blocks(card), text=fallback_text(card))
        channel = resp.get("channel") or self.channel
        ts = resp.get("ts")
        if not ts:
            raise AdapterError("Slack chat.postMessage returned no ts")
        return "%s:%s" % (channel, ts)

    def update(self, message_ref: str, card: dict) -> None:
        """Replace the card in place (new version, Granite arrival, decision recorded)."""
        channel, ts = _split_ref(message_ref)
        self._call("chat.update", self.client.chat_update,
                   channel=channel, ts=ts, blocks=blocks(card), text=fallback_text(card))

    def upload(self, message_ref: str, filename: str, text: str) -> None:
        """Attach a text file (the case packet) in the card's thread."""
        channel, ts = _split_ref(message_ref)
        self._call("files_upload_v2", self.client.files_upload_v2,
                   channel=channel, content=text, filename=filename, thread_ts=ts, title=filename)

    # ---- inbound

    def start(self, clicks: queue.Queue) -> None:
        """Register the button and modal handlers, then open the Socket Mode
        connection without blocking. Clicks arrive later as dicts on `clicks`."""
        self.clicks = clicks
        if self.app is None:
            if not self.bot_token:
                raise AdapterError("SLACK_BOT_TOKEN is not set; needed to receive clicks")
            try:
                from slack_bolt import App
            except ImportError as e:
                raise AdapterError("slack-bolt is not installed (pip install slack-bolt): %s" % e)
            # Bolt accepts only a real WebClient; reuse ours (with its 429 retry) when
            # it is one. Bolt takes the token from the client then, so pass only one.
            from slack_sdk import WebClient
            if isinstance(self.client, WebClient):
                self.app = App(client=self.client)
            else:
                self.app = App(token=self.bot_token)
        self._register(self.app)

        if not self.app_token:
            raise AdapterError("SLACK_APP_TOKEN is not set; put the app-level token (xapp-...) in .env")
        try:
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError as e:
            raise AdapterError("slack-bolt is not installed (pip install slack-bolt): %s" % e)
        self.handler = SocketModeHandler(self.app, self.app_token)
        self.handler.connect()

    def _register(self, app) -> None:
        """Wire the three buttons and the note modal to queue puts."""
        app.action(ACTION_IDS["freeze"])(self._on_freeze)
        app.action(ACTION_IDS["dismiss"])(self._on_dismiss)
        app.action(ACTION_IDS["more_info"])(self._on_more_info)
        app.view(NOTE_VIEW_ID)(self._on_note)

    def _put(self, ref: dict, decision: str, body: dict, note: str = "") -> None:
        user_id, user_name = _user(body)
        self.clicks.put({"ring_id": str(ref.get("ring_id") or ""),
                         "version": str(ref.get("version") or ""),
                         "decision": decision, "user_id": user_id,
                         "user_name": user_name, "note": note or ""})

    @staticmethod
    def _button_value(body: dict) -> dict:
        """The {ring_id, version} JSON carried by the clicked button."""
        try:
            value = (body.get("actions") or [{}])[0].get("value") or "{}"
            ref = json.loads(value)
            return ref if isinstance(ref, dict) else {}
        except (TypeError, ValueError, AttributeError, IndexError):
            return {}

    def _on_button(self, decision, ack, body):
        ack()
        ref = self._button_value(body)
        if not ref.get("ring_id"):
            log.warning("ignored a %s click with no ring in its value", decision)
            return
        self._put(ref, decision, body)

    def _on_freeze(self, ack, body, **_):
        self._on_button("freeze", ack, body)

    def _on_dismiss(self, ack, body, **_):
        self._on_button("dismiss", ack, body)

    def _on_more_info(self, ack, body, client=None, **_):
        """Open a modal asking for the note; the decision is sent when it is submitted."""
        ack()
        ref = self._button_value(body)
        if not ref.get("ring_id"):
            log.warning("ignored a more_info click with no ring in its value")
            return
        view = {
            "type": "modal",
            "callback_id": NOTE_VIEW_ID,
            "private_metadata": json.dumps({"ring_id": ref.get("ring_id"),
                                            "version": ref.get("version") or ""}),
            "title": {"type": "plain_text", "text": "Needs more info"},
            "submit": {"type": "plain_text", "text": "Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "section", "text": _mrkdwn(
                    "Ring *%s* v%s" % (esc(ref.get("ring_id")), esc(ref.get("version") or "?")))},
                {"type": "input", "block_id": NOTE_BLOCK_ID,
                 "label": {"type": "plain_text", "text": "What is needed?"},
                 "element": {"type": "plain_text_input", "action_id": NOTE_ACTION_ID,
                             "multiline": True, "max_length": 1000}},
            ],
        }
        web = client or self.client
        self._call("views.open", web.views_open, trigger_id=body.get("trigger_id"), view=view)

    def _on_note(self, ack, body, **_):
        """The modal's submission: the ring comes back in private_metadata, the note
        from the input block."""
        ack()
        view = (body or {}).get("view") or {}
        try:
            ref = json.loads(view.get("private_metadata") or "{}")
        except (TypeError, ValueError):
            ref = {}
        if not isinstance(ref, dict) or not ref.get("ring_id"):
            log.warning("ignored a note submission with no ring in its metadata")
            return
        try:
            note = view["state"]["values"][NOTE_BLOCK_ID][NOTE_ACTION_ID].get("value") or ""
        except (KeyError, TypeError, AttributeError):
            note = ""
        self._put(ref, "more_info", body, note.strip())

    def stop(self) -> None:
        """Close the Socket Mode connection, if one was opened."""
        if self.handler is not None:
            try:
                self.handler.close()
            except Exception as e:
                log.warning("closing the Slack connection failed: %s", type(e).__name__)
            self.handler = None


def _is_slack_error(e) -> bool:
    """True when `e` is slack_sdk's SlackApiError (checked lazily so this module
    imports without the library)."""
    try:
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return False
    return isinstance(e, SlackApiError)
