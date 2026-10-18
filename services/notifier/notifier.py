"""The notifier loop: read cases, post or update one card per ring, handle clicks.

Reads the `cases` stream in consumer group `notifier` (same pattern as the case
writer: BUSYGROUP tolerated, pending messages drained first, XACK after handling).
Keeps ring_id -> message record in memory and in the Redis hash `notifier:messages`,
so a restart posts no duplicates. A record is JSON:

    {"ref": <adapter reference>, "version": 3, "source": "granite",
     "reopened_for": <decided_at>, "suppressed_for": <decided_at>}

If the adapter raises (its chat product unreachable) the case is left pending and retried on a
later loop; a malformed case is logged once and acknowledged.
"""
import json
import sys
import threading
import time

from common import bus
from services.notifier import cards, decisions, packet, suppression

GROUP = "notifier"
MESSAGES = "notifier:messages"
BATCH = 10
BLOCK_MS = 5000
CASE_WINDOW = 5000     # how far back to look for one ring's case (clicks after a restart, --packet)
RING_VERSIONS_KEPT = 3


def log(text):
    print(text, flush=True)


def warn(text):
    print(text, file=sys.stderr, flush=True)


def find_case(r, ring_id, version=None, count=CASE_WINDOW):
    """The newest case for a ring (for one version if given) from the `cases` stream."""
    best = None
    for _msg_id, fields in r.xrevrange(bus.CASES, "+", "-", count=count):
        if (fields.get("ring_id") or "").strip() != ring_id:
            continue
        try:
            case = cards.parse(fields)
        except ValueError:
            continue
        if version not in (None, "") and case["version"] != cards.to_int(version):
            continue
        if best is None or cards.rank(case) > cards.rank(best):
            best = case
    return best


class RingCache:
    """The latest few versions of each ring from the `rings` stream (accounts and edges
    are not in the case message). Read incrementally: each refresh reads only what
    arrived since the last one."""

    def __init__(self, r):
        self.r = r
        self.last_id = "0"
        self.rings = {}   # ring_id -> {version: fields}

    def refresh(self, max_reads=50):
        for _ in range(max_reads):
            reply = self.r.xread({bus.RINGS: self.last_id}, count=1000)
            messages = reply[0][1] if reply else []
            if not messages:
                return
            for msg_id, fields in messages:
                self.last_id = msg_id
                ring_id = (fields.get("ring_id") or "").strip()
                version = cards.to_int(fields.get("version"), 0)
                if not ring_id:
                    continue
                versions = self.rings.setdefault(ring_id, {})
                versions[version] = fields
                for old in sorted(versions)[:-RING_VERSIONS_KEPT]:
                    del versions[old]

    def get(self, ring_id, version=None):
        """The ring message for this version, else the newest one we have, else None."""
        versions = self.rings.get(ring_id) or {}
        if not versions:
            return None
        version = cards.to_int(version)
        return versions.get(version) or versions[max(versions)]

    def accounts(self, ring_id):
        ring = self.get(ring_id)
        return packet.ring_parts(ring)[0] if ring else None


class Notifier:
    def __init__(self, r, adapter, consumer="notifier", hours=None, dashboard=None, block_ms=BLOCK_MS):
        self.r = r
        self.adapter = adapter
        self.consumer = consumer
        self.hours = suppression.suppress_hours() if hours is None else hours
        self.dashboard = dashboard or cards.dashboard_url()
        self.block_ms = block_ms
        self.records = {}
        self.cases = {}          # ring_id -> newest parsed case we handled
        self.facts_seen = {}     # (ring_id, version) -> facts, for the material-change check
        self.rings = RingCache(r)
        self.need_pending = True
        self.lock = threading.RLock()   # clicks arrive on the adapter's thread
        self.counts = {"posted": 0, "updated": 0, "suppressed": 0, "skipped": 0, "malformed": 0, "failed": 0}

    # --- start-up -----------------------------------------------------------------

    def restore(self):
        """Load ring_id -> message record from Redis so a restart re-posts nothing."""
        for ring_id, text in (self.r.hgetall(MESSAGES) or {}).items():
            try:
                record = json.loads(text)
                if isinstance(record, dict) and "ref" in record:
                    self.records[ring_id] = record
            except ValueError:
                warn("ignored unreadable %s entry for %s" % (MESSAGES, ring_id))
        return len(self.records)

    def ensure_group(self):
        import redis
        try:
            self.r.xgroup_create(bus.CASES, GROUP, id="0", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):   # group already exists = fine
                raise

    def start(self):
        self.ensure_group()
        n = self.restore()
        self.adapter.on_click(self.handle_click)
        log("notifier listening on '%s' (adapter: %s, %d cards restored, suppression %g h)" % (
            bus.CASES, self.adapter.name, n, self.hours))

    # --- the loop -----------------------------------------------------------------

    def run(self, max_loops=None, until_idle=False):
        """Read and handle cases. until_idle: stop when nothing is waiting (--once)."""
        import redis
        loops = 0
        while max_loops is None or loops < max_loops:
            loops += 1
            failed = self.counts["failed"]
            try:
                handled = self.poll(block_ms=None if until_idle else self.block_ms)
            except redis.exceptions.ConnectionError as exc:
                warn("redis not reachable (%s); retrying in 2 s" % exc)
                time.sleep(2)
                continue
            # --once: stop when idle, or when sending failed (the rest stays pending)
            if until_idle and (not handled or self.counts["failed"] > failed):
                break
        return self.counts

    def poll(self, block_ms=None):
        """One read: pending messages first when needed, then new ones. Returns how many
        messages were read."""
        read = 0
        if self.need_pending:
            reply = self.r.xreadgroup(GROUP, self.consumer, {bus.CASES: "0"}, count=100)
            messages = [m for _s, ms in reply or [] for m in ms if m[1]]
            # an entry with no fields was trimmed from the stream: nothing to redo
            for msg_id, fields in (m for _s, ms in reply or [] for m in ms if not m[1]):
                self.r.xack(bus.CASES, GROUP, msg_id)
            self.need_pending = len(messages) >= 100   # more pending than one read holds
            if messages:
                read += len(messages)
                self.handle_batch(messages)
        reply = self.r.xreadgroup(GROUP, self.consumer, {bus.CASES: ">"}, count=BATCH, block=block_ms)
        messages = [m for _s, ms in reply or [] for m in ms]
        if messages:
            read += len(messages)
            self.handle_batch(messages)
        return read

    def handle_batch(self, messages):
        """Coalesce to the newest case per ring, handle each, ack what is done."""
        newest = {}       # ring_id -> (case, msg_id)
        by_ring = {}      # ring_id -> every msg_id of that ring in the batch
        for msg_id, fields in messages:
            try:
                case = cards.parse(fields)
            except Exception as exc:
                self.counts["malformed"] += 1
                warn("skipped malformed case %s: %s" % (msg_id, exc))
                self.r.xack(bus.CASES, GROUP, msg_id)
                continue
            by_ring.setdefault(case["ring_id"], []).append(msg_id)
            if case["ring_id"] not in newest or cards.rank(case) >= cards.rank(newest[case["ring_id"]][0]):
                newest[case["ring_id"]] = (case, msg_id)

        if not newest:
            return
        self.rings.refresh()
        latest = decisions.latest(self.r)
        for ring_id, (case, _msg_id) in newest.items():
            self.facts_seen[(ring_id, case["version"])] = case["facts"]
            try:
                with self.lock:
                    self.handle_case(case, latest)
            except Exception as exc:
                # the chat product is unreachable (or refused): keep the messages pending
                self.counts["failed"] += 1
                self.need_pending = True
                warn("could not send %s v%s (%s); it stays pending and is retried" % (
                    ring_id, case["version"], exc))
                continue
            for msg_id in by_ring[ring_id]:
                self.r.xack(bus.CASES, GROUP, msg_id)

    # --- one case -----------------------------------------------------------------

    def dismissed_facts(self, ring_id, version):
        version = cards.to_int(version)
        if version is None:
            return None
        if (ring_id, version) in self.facts_seen:
            return self.facts_seen[(ring_id, version)]
        case = find_case(self.r, ring_id, version)
        return case["facts"] if case else None

    def card_for(self, case, latest, markers=(), footer_notes=(), decision=None):
        """The card for a case, with the ring's decision and any related-ring note."""
        decision = decision or latest.get(case["ring_id"])
        accounts = packet.ring_parts(self.rings.get(case["ring_id"], case["version"]))[0]
        notes = suppression.related_notes(case["ring_id"], accounts, case["written_at"],
                                          latest, self.rings.accounts, self.hours) if accounts else []
        return cards.build(case, decision=decision, markers=markers, notes=notes,
                           footer_notes=footer_notes, dashboard=self.dashboard)

    def handle_case(self, case, latest):
        ring_id, version = case["ring_id"], case["version"]
        record = self.records.get(ring_id)

        if record and cards.rank(case) <= (record.get("version", 0), 1 if record.get("source") == "granite" else 0):
            self.counts["skipped"] += 1
            return   # already showing this version or a newer one

        verdict = suppression.check(case, latest, self.dismissed_facts, self.hours,
                                    (record or {}).get("reopened_for", ""))
        if verdict["suppress"]:
            dismissal = verdict["dismissal"]
            self.counts["suppressed"] += 1
            log("suppressed %s v%s: %s" % (ring_id, version, suppression.describe(dismissal)))
            self.cases[ring_id] = case
            if record and record.get("suppressed_for") != dismissal.get("decided_at"):
                card = self.card_for(case, latest, footer_notes=[suppression.suppressed_footer(self.hours)],
                                     decision=dismissal)
                self.adapter.update(record["ref"], card)
                record.update(version=version, source=case["source"], suppressed_for=dismissal.get("decided_at"))
                self.save(ring_id, record)
            return

        markers = []
        if verdict["reopened"]:
            markers.append(verdict["reopened"])
        elif record and version > record.get("version", 0):
            markers.append("updated to v%s" % version)
        elif record and case["source"] == "granite":
            markers.append("Granite summary arrived")
        card = self.card_for(case, latest, markers)

        if record and not verdict["reopened"]:
            self.adapter.update(record["ref"], card)
            record.update(version=version, source=case["source"])
            self.counts["updated"] += 1
            log("updated %s to v%s (%s)" % (ring_id, version, case["source"]))
        else:
            # first card for this ring, or a dismissed ring that grew: a fresh message
            ref = self.adapter.post(card)
            record = {"ref": ref, "version": version, "source": case["source"],
                      "reopened_for": verdict["dismissal"].get("decided_at") if verdict["reopened"] else
                      (record or {}).get("reopened_for", "")}
            self.counts["posted"] += 1
            log("posted %s v%s (%s)%s" % (ring_id, version, case["source"],
                                          ", " + verdict["reopened"] if verdict["reopened"] else ""))
        self.cases[ring_id] = case
        self.save(ring_id, record)
        self.attach_packet(case, record)

    def save(self, ring_id, record):
        self.records[ring_id] = record
        self.r.hset(MESSAGES, ring_id, json.dumps(record))

    def attach_packet(self, case, record):
        """The case packet as a file on the card. A failed upload is logged, not retried:
        the card itself was sent, and retrying would send it twice."""
        if not self.adapter.can_upload:
            return
        try:
            text = packet.render(case, decisions.history(self.r, case["ring_id"]),
                                 self.rings.get(case["ring_id"], case["version"]))
            self.adapter.upload(record["ref"], packet.filename(case), text)
        except Exception as exc:
            warn("case packet for %s not attached: %s" % (case["ring_id"], exc))

    # --- clicks -------------------------------------------------------------------

    def handle_click(self, ring_id, version, decision, note, decided_by, channel):
        """A button press (or a typed command). Writes one decision and redraws the card.
        A second decision on a ring already decided for this version is ignored."""
        ring_id = (ring_id or "").strip()
        with self.lock:
            record = self.records.get(ring_id)
            case = self.cases.get(ring_id) or find_case(self.r, ring_id)
            if version in (None, ""):
                version = (record or {}).get("version") or (case or {}).get("version") or ""
            latest = decisions.latest(self.r)
            previous = latest.get(ring_id)
            if cards.decided_for(previous, version):
                return {"ok": False, "message": "%s already has a decision: %s" % (
                    ring_id, cards.decision_view(previous)["text"])}
            try:
                fields = decisions.write(self.r, ring_id, version, decision, note, decided_by, channel)
            except decisions.DecisionError as exc:
                return {"ok": False, "message": str(exc)}
            latest[ring_id] = fields
            log("decision %s on %s v%s by %s" % (fields["decision"], ring_id, fields["version"], decided_by))
            card = None
            if case:
                self.rings.refresh()
                card = self.card_for(case, latest)
                if record:
                    try:
                        self.adapter.update(record["ref"], card)
                    except Exception as exc:
                        warn("card for %s not redrawn: %s" % (ring_id, exc))
            return {"ok": True, "message": cards.decision_view(fields)["text"], "card": card}
