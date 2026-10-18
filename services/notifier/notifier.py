"""The reviewer bot's loop: new cases become chat cards, clicks become decisions.

    from services.notifier.notifier import Notifier
    n = Notifier(r, adapter, dashboard_url, suppress_hours)
    n.run()                       # what main.py does

What happens to one `cases` message:
1. It is read with the consumer group "notifier" (pending messages first after a
   restart, then new ones), the same pattern as the case writer.
2. Within one batch only the newest case per ring is kept (highest version, Granite
   over template at the same version); the others are acknowledged as superseded.
3. suppression.decide() says post, update, suppress or skip, from the latest
   decision on `decisions`, what was posted before (the `notifier:messages` hash) and
   the latest `rings` message (for the accounts and the related-ring note).
4. The card is built by cards.build() and handed to the adapter. One chat message per
   ring: later versions and the Granite upgrade edit it in place.
5. If services/notifier/packet.py is present, the case packet is attached.
6. The snapshot is saved to `notifier:messages` and the case is acknowledged.

Failure rules: a malformed case is logged once and acknowledged (it will never get
better). A case whose post failed with AdapterError is NOT acknowledged, so it is
read again from the pending list and sent when the chat product is back. Redis
connection errors are logged and retried after a second.

Clicks arrive on a queue filled by the adapter. Each click writes one decision
(services/notifier/decisions.py) and redraws the card with the status and disabled
buttons. A second click on a ring that already has a decision is logged and ignored,
so the audit trail never holds two answers to the same card.
"""
import importlib
import json
import logging
import queue
import socket
import time
from datetime import datetime, timezone

from common import bus
from services.notifier import cards, decisions, suppression
from services.notifier.adapters.base import AdapterError

log = logging.getLogger("notifier")

GROUP = "notifier"
MESSAGES = "notifier:messages"   # hash: ring_id -> JSON snapshot of the card last sent
BATCH = 10
PENDING_BATCH = 100
RINGS_WINDOW = 2000
CASES_WINDOW = 500


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def case_rank(fields):
    """Which case wins for a ring: highest version, then Granite over template."""
    return (cards.to_int(fields.get("version")) or 0,
            1 if str(fields.get("source") or "").strip().lower() == "granite" else 0)


def decided_by(channel, user_id, user_name):
    """'slack', 'U0123ABC', 'jane.doe' -> 'slack:U0123ABC:jane.doe'; console -> 'console:aryan'."""
    user_id, user_name = str(user_id or "").strip(), str(user_name or "").strip() or "unknown"
    if not user_id or user_id == channel:
        return "%s:%s" % (channel, user_name)
    return "%s:%s:%s" % (channel, user_id, user_name)


def adapter_kind(adapter):
    """"slack" | "teams" | "console": the adapter's `kind`, else guessed from its class name."""
    kind = str(getattr(adapter, "kind", "") or "").strip().lower()
    if kind in decisions.CHANNELS:
        return kind
    name = type(adapter).__name__.lower()
    return next((k for k in ("slack", "teams") if k in name), "console")


def _flatten(reply):
    return [(msg_id, fields or {}) for _stream, messages in reply or [] for msg_id, fields in messages]


class Notifier:
    def __init__(self, r, adapter, dashboard_url="", suppress_hours=24.0, group=GROUP, consumer=None):
        self.r = r
        self.adapter = adapter
        self.dashboard_url = dashboard_url or ""
        self.suppress_hours = float(suppress_hours)
        self.group = group
        self.consumer = consumer or socket.gethostname()
        self.channel = adapter_kind(adapter)
        self.clicks = queue.Queue()
        self.messages = {}   # ring_id -> snapshot (mirror of the notifier:messages hash)
        self.cases = {}      # ring_id -> the case fields on the card (memory only)
        self.rings = {}      # ring_id -> the ring fields on the card (memory only)
        self.retry_seconds = 5.0
        self._read_pending = True
        self._retry_at = None
        self._ready = False
        self._logged_bad = set()

    # --- setup -----------------------------------------------------------------

    def ensure_group(self):
        import redis
        try:
            self.r.xgroup_create(bus.CASES, self.group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):   # group already exists = fine
                raise

    def restore(self):
        """Load what was posted before a restart, so nothing is posted twice."""
        self.messages = {}
        for ring_id, text in (self.r.hgetall(MESSAGES) or {}).items():
            try:
                snap = json.loads(text)
            except (TypeError, ValueError):
                log.warning("ignored unreadable %s entry for %s", MESSAGES, ring_id)
                continue
            if isinstance(snap, dict) and snap.get("ref"):
                self.messages[ring_id] = snap
        if self.messages:
            log.info("restored %d posted cards from %s", len(self.messages), MESSAGES)
        return self.messages

    def _setup(self):
        if not self._ready:
            self.ensure_group()
            self.restore()
            self._ready = True
            self._read_pending = True   # after a start or a reconnect, drain our pending list first

    def _save(self, ring_id, snap):
        self.messages[ring_id] = snap
        self.r.hset(MESSAGES, ring_id, json.dumps(snap))

    # --- reading other streams ---------------------------------------------------

    def rings_index(self, count=RINGS_WINDOW):
        """ring_id -> the latest `rings` message (highest version; newest on a tie),
        with `accounts` already parsed into a list."""
        index, best = {}, {}
        for _msg_id, f in self.r.xrevrange(bus.RINGS, "+", "-", count=count):
            ring_id = (f or {}).get("ring_id")
            if not ring_id:
                continue
            version = cards.to_int(f.get("version")) or 0
            if ring_id in best and best[ring_id] >= version:
                continue
            accounts = cards.json_field(f.get("accounts"), [])
            fields = dict(f)
            fields["accounts"] = [str(a) for a in accounts] if isinstance(accounts, list) else []
            index[ring_id], best[ring_id] = fields, version
        return index

    def _latest_case(self, ring_id):
        """The case on the card; memory first, else the best one on `cases` (after a restart)."""
        if ring_id in self.cases:
            return self.cases[ring_id]
        best = None
        for _msg_id, f in self.r.xrevrange(bus.CASES, "+", "-", count=CASES_WINDOW):
            if (f or {}).get("ring_id") == ring_id and (best is None or case_rank(f) > case_rank(best)):
                best = f
        return best

    # --- cases -----------------------------------------------------------------

    def _bad(self, msg_id, why):
        if msg_id not in self._logged_bad:
            self._logged_bad.add(msg_id)
            log.warning("skipped malformed case %s: %s", msg_id, why)

    def _ack(self, *msg_ids):
        if msg_ids:
            self.r.xack(bus.CASES, self.group, *msg_ids)

    def handle_cases(self, messages):
        """Handle one batch of (msg_id, fields). Returns {ring_id: action}."""
        groups = {}
        for pos, (msg_id, fields) in enumerate(messages):
            ring_id = (fields or {}).get("ring_id")
            if not ring_id:
                self._bad(msg_id, "no ring_id")
                self._ack(msg_id)
                continue
            groups.setdefault(ring_id, []).append((case_rank(fields), pos, msg_id, fields))
        if not groups:
            return {}

        index = self.rings_index()
        accounts_index = {k: v["accounts"] for k, v in index.items()}
        latest = decisions.latest(self.r)
        done = {}
        for ring_id, items in groups.items():
            items.sort()
            _rank, _pos, msg_id, fields = items[-1]
            self._ack(*[m for _r, _p, m, _f in items[:-1]])   # superseded within the batch
            try:
                done[ring_id] = self._handle_one(fields, index.get(ring_id), latest, accounts_index)
            except AdapterError as exc:
                log.warning("could not send %s v%s, will retry: %s", ring_id, fields.get("version"), exc)
                self._retry_at = time.time() + self.retry_seconds
                done[ring_id] = "retry"
                continue   # not acknowledged: stays pending
            except Exception as exc:
                if _is_redis_down(exc):
                    raise
                self._bad(msg_id, exc)
                done[ring_id] = "bad"
            self._ack(msg_id)
        return done

    def _handle_one(self, fields, ring, latest, accounts_index):
        ring_id, version = fields["ring_id"], str(fields.get("version") or "")
        source = str(fields.get("source") or "").strip().lower()
        snap = self.messages.get(ring_id)
        action, markers = suppression.decide(fields, ring, latest, self.messages,
                                             accounts_index, self.suppress_hours)
        if action == "skip":
            log.info("skipped %s v%s (%s): the card already shows v%s (%s)", ring_id, version, source,
                     (snap or {}).get("version"), (snap or {}).get("source"))
            return action
        if action == "suppress":
            self._suppress(fields, ring, latest.get(ring_id), snap, markers)
            return action

        decision = suppression.active_decision(latest.get(ring_id), snap)
        reopened = action == "post" and decision is not None and decision.get("decision") == "dismiss"
        card = cards.build(fields, ring, None if reopened else decision, markers, self.dashboard_url)
        if action == "post":
            ref = self.adapter.post(card)
        else:
            ref = snap["ref"]
            self.adapter.update(ref, card)

        new = {"ref": ref, "version": version, "source": source, "n_accounts": card["n_accounts"],
               "total_usd": card["total_usd"], "markers": card["markers"],
               "posted_at": _now_iso() if action == "post" else snap.get("posted_at", "")}
        if action == "update":
            new["updated_at"] = _now_iso()
        if reopened:
            new["reopened_after"] = decision.get("decided_at", "")
        elif action == "update" and snap.get("reopened_after"):
            new["reopened_after"] = snap["reopened_after"]
        self._save(ring_id, new)
        self.cases[ring_id], self.rings[ring_id] = fields, ring
        log.info("%s %s v%s (%s) as %s%s", "posted" if action == "post" else "updated", ring_id, version,
                 source, ref, (" [%s]" % "; ".join(markers)) if markers else "")
        self._upload_packet(ref, fields, ring)
        return action

    def _suppress(self, fields, ring, decision, snap, markers):
        """Acknowledge and log; redraw the existing card once with a 'dismissed' footer."""
        # the marker reads "suppressed v5: dismissed by jane at ..."; the log line adds the ring
        why = markers[0].split(": ", 1)[-1] if markers else "dismissed"
        log.info("suppressed %s v%s: %s", fields["ring_id"], fields.get("version"), why)
        if not snap or not snap.get("ref") or snap.get("suppressed_for") == (decision or {}).get("decided_at"):
            return
        card = cards.build(fields, ring, decision, markers, self.dashboard_url)
        try:
            self.adapter.update(snap["ref"], card)
        except AdapterError as exc:
            log.warning("could not mark %s as dismissed on its card: %s", fields["ring_id"], exc)
            return
        snap = dict(snap, suppressed_for=(decision or {}).get("decided_at", ""))
        self._save(fields["ring_id"], snap)

    def _upload_packet(self, ref, fields, ring):
        """Attach the case packet when services/notifier/packet.py exists. Never fatal."""
        try:
            packet = importlib.import_module("services.notifier.packet")
        except ImportError:
            return
        except Exception as exc:   # a broken packet module must not stop the cards
            log.warning("packet module failed to load: %s", exc)
            return
        ring_id, version = fields["ring_id"], fields.get("version", "")
        try:
            text = packet.render(fields, ring, decisions.history(self.r, ring_id), manifest=None)
            self.adapter.upload(ref, "case_%s_v%s.md" % (ring_id, version), text)
        except Exception as exc:
            if _is_redis_down(exc):
                raise
            log.warning("could not attach the packet for %s v%s: %s", ring_id, version, exc)

    # --- clicks ----------------------------------------------------------------

    def handle_clicks(self):
        """Drain the click queue. Returns the list of outcomes ("written", "ignored", "invalid")."""
        out = []
        while True:
            try:
                click = self.clicks.get_nowait()
            except queue.Empty:
                return out
            try:
                out.append(self._handle_click(click))
            except Exception as exc:
                if _is_redis_down(exc):
                    self.clicks.put(click)   # try again once Redis is back
                    raise
                log.warning("click failed %r: %s", click, exc)
                out.append("invalid")

    def _handle_click(self, click):
        ring_id = str(click.get("ring_id") or "").strip()
        snap = self.messages.get(ring_id)
        current = suppression.active_decision(decisions.latest(self.r).get(ring_id), snap)
        if current is not None:
            log.info("ignored %s on %s by %s: already %s", click.get("decision"), ring_id,
                     click.get("user_name"), cards.status_of(current)["label"])
            return "ignored"
        version = str(click.get("version") or "") or (snap or {}).get("version") or None
        who = decided_by(self.channel, click.get("user_id"), click.get("user_name"))
        try:
            msg = decisions.build(ring_id, version, click.get("decision"), click.get("note") or "",
                                  who, self.channel)
        except decisions.DecisionError as exc:
            log.warning("rejected click on %s by %s: %s", ring_id, who, exc)
            return "invalid"
        self.r.xadd(bus.DECISIONS, msg)
        log.info("decision %s on %s v%s by %s", msg["decision"], ring_id, msg["version"], who)
        if snap and snap.get("ref"):
            self._redraw(ring_id, snap, msg)
        return "written"

    def _redraw(self, ring_id, snap, decision):
        case = self._latest_case(ring_id)
        if case is None:
            return
        ring = self.rings.get(ring_id) or self.rings_index().get(ring_id)
        card = cards.build(case, ring, decision, snap.get("markers") or [], self.dashboard_url)
        try:
            self.adapter.update(snap["ref"], card)
        except AdapterError as exc:
            log.warning("decision written but the card for %s was not redrawn: %s", ring_id, exc)

    # --- the loop ----------------------------------------------------------------

    def run_once(self, block_ms=1000):
        """One read of `cases` (the pending list first after a start or a failed send,
        then new messages), then the clicks. Returns how many cases were read."""
        self._setup()
        n = 0
        if self._read_pending or (self._retry_at is not None and time.time() >= self._retry_at):
            self._read_pending, self._retry_at = False, None
            pending = _flatten(self.r.xreadgroup(self.group, self.consumer, {bus.CASES: "0"},
                                                 count=PENDING_BATCH))
            if pending:
                self.handle_cases(pending)
                n = len(pending)
        if n == 0:
            fresh = _flatten(self.r.xreadgroup(self.group, self.consumer, {bus.CASES: ">"},
                                               count=BATCH, block=block_ms or None))
            if fresh:
                self.handle_cases(fresh)
                n = len(fresh)
        self.handle_clicks()
        return n

    def run(self, stop_event=None, block_ms=1000):
        """Run until stop_event is set or the console says quit. Never exits on a Redis error."""
        import redis
        try:
            self.adapter.start(self.clicks)
        except AdapterError as exc:
            log.warning("the %s adapter cannot receive clicks (%s); cards are still sent, "
                        "decide on the dashboard", self.channel, exc)
        quit_event = getattr(self.adapter, "quit_event", None)
        log.info("notifier listening on '%s' (group %s, adapter %s, suppress %g h)",
                 bus.CASES, self.group, self.channel, self.suppress_hours)
        try:
            while not (stop_event is not None and stop_event.is_set()):
                if quit_event is not None and quit_event.is_set():
                    break
                try:
                    self.run_once(block_ms)
                except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
                    log.warning("Redis not reachable (%s); retrying in 1 s", exc.__class__.__name__)
                    self._ready = False
                    time.sleep(1)
                except redis.exceptions.ResponseError as exc:
                    log.warning("Redis error: %s; retrying in 1 s", exc)
                    self._ready = False   # NOGROUP after a flush: create the group again
                    time.sleep(1)
        finally:
            self.adapter.stop()


def _is_redis_down(exc):
    try:
        import redis
    except ImportError:
        return False
    return isinstance(exc, (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError))
