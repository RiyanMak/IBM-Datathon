"""
Step 5 - the case writer service.

Reads rings from the Redis stream `rings`, writes cases to the stream `cases`.
Each case is also kept in the hash `case:<ring_id>` (always the latest for that
ring, so the page reads one hash instead of scanning the stream), and the running
counts are kept in the hash `stats:casewriter`.

How to run (from the repo root):
  python -m services.casewriter.main --file services/casewriter/fake_rings.json       # no Redis: prints cases
  python -m services.casewriter.main --push-fake services/casewriter/fake_rings.json  # put fake rings on Redis
  python -m services.casewriter.main                                                   # the real service (same as --redis)

Redis host and stream names come from common/bus.py (REDIS_HOST, default "redis").

Granite is used automatically when the watsonx settings exist (see granite.py).
Add --no-granite to force the template.

When Granite runs (GRANITE_MODE):
  on_demand (default)  every ring gets the template at once; Granite writes for a ring
                       when someone opens it on the page (a message on `granite_requests`),
                       and keeps that ring fresh for a few minutes afterwards. The demo
                       never queues behind rings nobody is looking at.
  all                  Granite also works through every other ring in the background,
                       after the requested ones.
GRANITE_WORKERS (default 3) Granite calls run side by side.
"""
import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from common import bus
from services.casewriter.facts import parse_ring, build_facts
from services.casewriter.template import template_summary
from services.casewriter.factcheck import check_reply
from services.casewriter import granite

IN_STREAM = bus.RINGS
OUT_STREAM = bus.CASES
REQUESTS_STREAM = bus.GRANITE_REQUESTS
GROUP = "casewriter"
CASE_KEY = "case:%s"              # latest case per ring, same fields as the stream message
STATS_KEY = "stats:casewriter"    # cases_written, granite_tried, granite_passed

MODE_ON_DEMAND, MODE_ALL = "on_demand", "all"
PRIORITY_REQUEST, PRIORITY_BACKGROUND = 0, 1   # a requested ring goes before every background one
WATCH_SECONDS = 180      # after a request, new versions of that ring keep getting Granite for this long
RING_SCAN = 5000         # how far back the rings stream is searched for a requested ring not in memory

# Notes the template case carries while Granite has not written (the page reads them)
NOTE_PENDING = "granite pending"         # queued or being written now
NOTE_ON_REQUEST = "granite on request"   # on_demand mode: Granite writes when someone opens the ring


def granite_mode():
    value = os.environ.get("GRANITE_MODE", MODE_ON_DEMAND).strip().lower()
    return MODE_ALL if value in (MODE_ALL, "every", "background") else MODE_ON_DEMAND

# Facts that move with the clock alone. Two facts packs that differ only in these keys
# tell the same story, so the previous summary is reused instead of asking Granite again.
# Every other time fact (first_seen, hours_to_pass_on, hours_to_pass_on_all) comes from
# edge times and only changes together with the edges, so a change there is a new story.
TIME_ONLY_FACTS = ("last_seen", "duration_hours", "version")


class Stats:
    """Counts how often Granite passes the fact check - a number for the pitch."""
    def __init__(self):
        self.cases = 0
        self.granite_tried = 0
        self.granite_passed = 0

    def line(self):
        return "cases written: %d | Granite passed the fact check in %d of %d tries" % (
            self.cases, self.granite_passed, self.granite_tried)

    def fields(self):
        """The counters as the stats:casewriter hash holds them (text, like every Redis value)."""
        return {"cases_written": str(self.cases), "granite_tried": str(self.granite_tried),
                "granite_passed": str(self.granite_passed)}


def version_key(version):
    """Versions arrive as text; compare them as numbers ('10' is newer than '9')."""
    try:
        return int(float(version))
    except (TypeError, ValueError):
        return -1


def same_story(facts):
    """The facts pack without the keys that only move with the clock."""
    return {k: v for k, v in facts.items() if k not in TIME_ONLY_FACTS}


def seconds_since(msg_id, now):
    """Wall-clock seconds from a Redis stream ID ('1697040000123-0') to `now`; '' without an ID."""
    if not msg_id:
        return ""
    try:
        sent_ms = int(str(msg_id).split("-", 1)[0])
    except ValueError:
        return ""
    return "%.3f" % max(0.0, now - sent_ms / 1000.0)


def case_message(ring, facts, summary, source, note, msg_id=None):
    """One case message, all values as text. `msg_id` is the ring's stream ID ('' or None
    in --file mode), used for the case writer's own latency."""
    now = datetime.now(timezone.utc)
    return {
        "ring_id": ring["ring_id"],
        "version": ring["version"],
        "summary": json.dumps(summary),
        "source": source,
        "check_note": note,
        "written_at": now.isoformat(timespec="seconds"),
        "facts": json.dumps(facts),   # extra field: lets the page show the numbers
        "ring_last_seen": ring.get("last_seen") or facts["last_seen"],   # data time
        "written_after_s": seconds_since(msg_id, now.timestamp()),
    }


def write_case(fields, use_granite, stats=None, granite_fn=None, msg_id=None):
    """Turn one ring message into one case message (a dict of text values)."""
    granite_fn = granite_fn or granite.call_granite
    ring = parse_ring(fields)
    facts = build_facts(ring)
    summary = template_summary(facts)
    source, note = "template", "granite not used"

    if use_granite:
        if stats:
            stats.granite_tried += 1
        try:
            reply = granite.parse_reply(granite_fn(facts))
            ok, why = check_reply(reply, facts, ring["accounts"])
            if ok:
                summary = {k: reply[k] for k in summary}  # keep only the 5 parts
                source, note = "granite", "passed fact check"
                if stats:
                    stats.granite_passed += 1
            else:
                note = "granite reply rejected: " + why
        except granite.GraniteDisabled as exc:
            note = str(exc)
        except Exception as exc:  # network error, bad JSON, timeout...
            note = "granite error: %s" % str(exc)[:200]

    if stats:
        stats.cases += 1
    return case_message(ring, facts, summary, source, note, msg_id)


def try_granite(fields, stats=None, granite_fn=None, msg_id=None):
    """Try Granite for one ring. Returns (case, note): the upgraded case message
    (source "granite") and None, or None and why it did not pass.

    Used by the service after the template case has already been published, so a slow
    model never delays the page: the template shows first and Granite replaces it.
    """
    granite_fn = granite_fn or granite.call_granite
    ring = parse_ring(fields)
    facts = build_facts(ring)
    template = template_summary(facts)
    if stats:
        stats.granite_tried += 1
    try:
        reply = granite.parse_reply(granite_fn(facts))
        ok, why = check_reply(reply, facts, ring["accounts"])
    except granite.GraniteDisabled as exc:
        return None, str(exc)
    except Exception as exc:  # network error, bad JSON, timeout, model error...
        note = "granite error: %s" % str(exc)[:200]
        print("%s for %s v%s" % (note, ring["ring_id"], ring["version"]), file=sys.stderr)
        return None, note
    if not ok:
        note = "granite reply rejected: " + why
        print("%s for %s v%s" % (note, ring["ring_id"], ring["version"]), file=sys.stderr)
        return None, note
    if stats:
        stats.granite_passed += 1
    case = case_message(ring, facts, {k: reply[k] for k in template}, "granite",
                        "passed fact check", msg_id)
    return case, None


def granite_case(fields, stats=None, granite_fn=None):
    """The Granite case for one ring, or None when the call failed or the reply failed
    the fact check (the reason is printed)."""
    return try_granite(fields, stats, granite_fn)[0]


def ring_to_stream_fields(ring):
    """Fake ring (Python dict) -> text values, exactly as the ring service sends them."""
    out = {}
    for k, v in ring.items():
        out[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
    return out


def run_file(path, use_granite):
    stats = Stats()
    with open(path) as fh:
        rings = json.load(fh)
    for ring in rings:
        case = write_case(ring_to_stream_fields(ring), use_granite, stats)
        print("=" * 70)
        print("ring %s  (source: %s, %s)" % (case["ring_id"], case["source"], case["check_note"]))
        for part, text in json.loads(case["summary"]).items():
            print("\n%s:\n  %s" % (part, text))
    print("=" * 70)
    print(stats.line())


def connect_redis():
    return bus.connect()


def push_fake(path, r=None):
    r = r or connect_redis()
    with open(path) as fh:
        rings = json.load(fh)
    for ring in rings:
        r.xadd(IN_STREAM, ring_to_stream_fields(ring))
    print("pushed %d fake rings onto '%s'" % (len(rings), IN_STREAM))


def _case_rank(version, source):
    """Which case the hash keeps: the higher version; at the same version Granite beats
    the template (the template is written first, Granite seconds later)."""
    return (version_key(version), 1 if source == "granite" else 0)


def store_case(r, case):
    """Append a case to the `cases` stream and keep `case:<ring_id>` on the latest one.

    The stream is the audit trail and takes every case. The hash only moves forward,
    so a slow Granite reply for an older version never replaces a newer case.
    """
    r.xadd(OUT_STREAM, case)
    key = CASE_KEY % case["ring_id"]
    held = r.hmget(key, "version", "source")
    if held[0] is None or _case_rank(case["version"], case["source"]) >= _case_rank(*held):
        r.hset(key, mapping=case)


class Publisher:
    """Writes cases and counters to Redis and remembers the latest story per ring.

    Shared by the main loop and the Granite worker thread, so every write and every
    look at the memory goes through one lock.
    """
    def __init__(self, r, stats):
        self.r, self.stats = r, stats
        self.lock = threading.Lock()
        # ring_id -> {"story": facts without clock-only keys, "case": the best case for
        # that story, "from_version": the version that case's summary was written for,
        # "latest": the last case published for the ring, "msg_id": its ring message ID,
        # "fields": the ring message as received (what a Granite request is served from)}
        self.rings = {}
        self.watched = {}   # ring_id -> wall time of the last request from the page

    def write_stats(self):
        self.r.hset(STATS_KEY, mapping=self.stats.fields())

    def is_watched(self, ring_id, now=None):
        """True while someone asked for this ring recently (new versions get Granite first)."""
        asked = self.watched.get(ring_id)
        return asked is not None and (now or time.time()) - asked < WATCH_SECONDS

    def request(self, req, now=None):
        """Serve one `granite_requests` message. Returns (fields, msg_id) of the ring to
        send to Granite, or None when there is nothing to do: the ring's current story
        already has a Granite summary, or the ring is unknown."""
        ring_id = str(req.get("ring_id") or "").strip()
        if not ring_id:
            return None
        now = now or time.time()
        with self.lock:
            self.watched[ring_id] = now
            mem = self.rings.get(ring_id)
            if mem is not None:
                if mem["latest"]["source"] == "granite":
                    return None   # the newest case already carries Granite's text
                fields, msg_id = mem["fields"], mem["msg_id"]
            else:
                found = self._find_ring(ring_id)
                if found is None:
                    print("granite request for unknown ring %s" % ring_id, file=sys.stderr)
                    return None
                msg_id, fields = found
            key = CASE_KEY % ring_id
            if self.r.hmget(key, "source") == ["template"]:
                self.r.hset(key, "check_note", NOTE_PENDING)
        return fields, msg_id

    def _find_ring(self, ring_id):
        """The newest message for a ring on the rings stream (after a restart the memory
        is empty), or None."""
        for msg_id, fields in self.r.xrevrange(IN_STREAM, "+", "-", count=RING_SCAN):
            if fields.get("ring_id") == ring_id:
                return msg_id, fields
        return None

    def _reuse(self, best, from_version, latest, msg_id):
        """The summary of `best`, re-sent on the ring version of `latest`."""
        case = dict(latest)
        now = datetime.now(timezone.utc)
        case.update(summary=best["summary"], source=best["source"],
                    check_note="reused v%s summary" % from_version,
                    written_at=now.isoformat(timespec="seconds"),
                    written_after_s=seconds_since(msg_id, now.timestamp()))
        return case

    def template_first(self, msg_id, fields, use_granite, mode=MODE_ALL):
        """Publish a case for one ring message at once. Returns the priority Granite should
        be tried with (new or changed facts, Granite on and not disabled), or None when
        the template stands: nothing changed, Granite is off, or on_demand mode and nobody
        has asked for this ring."""
        ring = parse_ring(fields)
        facts = build_facts(ring)
        story = same_story(facts)
        with self.lock:
            prev = self.rings.get(ring["ring_id"])
            if prev is not None and prev["story"] == story:
                # nothing but the clock moved: the summary already written still holds
                latest = case_message(ring, facts, {}, "", "", msg_id)
                case = self._reuse(prev["case"], prev["from_version"], latest, msg_id)
                prev.update(latest=case, msg_id=msg_id, fields=fields)
                priority = None
            else:
                watched = self.is_watched(ring["ring_id"])
                if not use_granite:
                    note, priority = "granite not used", None
                elif granite.disabled_reason():
                    note, priority = "granite disabled: " + granite.disabled_reason(), None
                elif watched:
                    note, priority = NOTE_PENDING, PRIORITY_REQUEST
                elif mode == MODE_ON_DEMAND:
                    note, priority = NOTE_ON_REQUEST, None
                else:
                    note, priority = NOTE_PENDING, PRIORITY_BACKGROUND
                case = case_message(ring, facts, template_summary(facts), "template", note, msg_id)
                self.rings[ring["ring_id"]] = {"story": story, "case": case,
                                               "from_version": ring["version"],
                                               "latest": case, "msg_id": msg_id, "fields": fields}
            self.stats.cases += 1
            store_case(self.r, case)
            self.write_stats()
        print("case for %s v%s (%s, %s)" % (case["ring_id"], case["version"], case["source"],
                                           case["check_note"]))
        return priority

    def granite_result(self, fields, case, note):
        """Publish what the Granite worker found for one ring message."""
        ring_id, version = str(fields.get("ring_id")), str(fields.get("version") or "1")
        with self.lock:
            if case is None:
                # keep the hash honest about why the template stays (the stream keeps
                # the original message, so the audit trail is unchanged)
                key = CASE_KEY % ring_id
                if note and self.r.hmget(key, "version", "source") == [version, "template"]:
                    self.r.hset(key, "check_note", note)
                self.write_stats()
                return
            store_case(self.r, case)
            print("case for %s v%s (granite, replaced the template)" % (ring_id, version))
            mem = self.rings.get(ring_id)
            if mem is not None and mem["story"] == same_story(json.loads(case["facts"])):
                mem["case"], mem["from_version"] = case, version
                if version_key(mem["latest"]["version"]) == version_key(version):
                    mem["latest"] = case   # so a repeat request sees the ring is already done
                if version_key(mem["latest"]["version"]) > version_key(version):
                    # newer versions with the same story went out while Granite worked;
                    # give the newest one the Granite text too
                    later = self._reuse(case, version, mem["latest"], mem["msg_id"])
                    mem["latest"] = later
                    store_case(self.r, later)
                    print("case for %s v%s (granite, reused v%s summary)" % (ring_id, later["version"], version))
            self.write_stats()


class GraniteWorker:
    """Runs Granite for rings on a few threads and publishes the upgraded cases.

    The service publishes the template case at once, then hands the ring to this
    worker. Requested rings (someone opened them on the page) go before background
    ones, so a request is answered in one Granite call's time rather than after the
    whole backlog. The same ring version is never in the queue twice. A ring that
    arrives while the queue is full keeps its template (noted in the log).
    """
    QUEUE_SIZE = 200

    def __init__(self, r, stats, granite_fn=None, publisher=None, workers=None):
        self.r, self.stats, self.granite_fn = r, stats, granite_fn
        self.publisher = publisher or Publisher(r, stats)
        self.queue = queue.PriorityQueue(maxsize=self.QUEUE_SIZE)
        self.lock = threading.Lock()
        self.seq = 0                 # keeps the queue first-in-first-out within a priority
        self.in_flight = set()       # (ring_id, version) queued or being written
        count = workers if workers is not None else int(os.environ.get("GRANITE_WORKERS", "3") or 1)
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(max(1, count))]
        for t in self.threads:
            t.start()

    def submit(self, fields, msg_id=None, priority=PRIORITY_BACKGROUND):
        key = (str(fields.get("ring_id")), str(fields.get("version") or "1"))
        with self.lock:
            if key in self.in_flight:
                return False
            self.in_flight.add(key)
            self.seq += 1
            item = (priority, self.seq, fields, msg_id)
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            with self.lock:
                self.in_flight.discard(key)
            print("granite queue full: %s v%s keeps the template" % key, file=sys.stderr)
            return False

    def _run(self):
        while True:
            _priority, _seq, fields, msg_id = self.queue.get()
            if fields is None:
                self.queue.task_done()
                return
            try:
                case, note = try_granite(fields, self.stats, self.granite_fn, msg_id)
                self.publisher.granite_result(fields, case, note)
            except Exception as exc:
                print("granite worker error: %s" % exc, file=sys.stderr)
            finally:
                with self.lock:
                    self.in_flight.discard((str(fields.get("ring_id")), str(fields.get("version") or "1")))
                self.queue.task_done()

    def drain(self):
        """Wait for every queued ring (tests and shutdown)."""
        self.queue.join()


class RequestListener:
    """Reads `granite_requests` (the page writes one when an investigator opens a ring)
    and hands each ring to the worker ahead of the background queue.

    Only requests written after the service started count: an old request is for a
    ring whose version has moved on, and the page asks again when it is still open.
    """
    def __init__(self, r, publisher, worker, start_id="$"):
        self.r, self.publisher, self.worker = r, publisher, worker
        self.last_id = start_id
        self.stop = False
        self.thread = None

    def poll(self, block_ms=None):
        """Serve the requests that arrived since the last call; returns how many were queued."""
        reply = self.r.xread({REQUESTS_STREAM: self.last_id}, count=50, block=block_ms)
        queued = 0
        for _stream, messages in reply or []:
            for msg_id, fields in messages:
                self.last_id = msg_id
                found = self.publisher.request(fields)
                if found is not None:
                    ring_fields, ring_msg_id = found
                    if self.worker.submit(ring_fields, ring_msg_id, PRIORITY_REQUEST):
                        queued += 1
                        print("granite requested for %s v%s by %s" % (
                            ring_fields.get("ring_id"), ring_fields.get("version"),
                            fields.get("requested_by") or "page"))
        return queued

    def _run(self):
        while not self.stop:
            try:
                self.poll(block_ms=1000)
            except Exception as exc:   # Redis hiccup: wait a moment and keep listening
                print("granite request listener: %s" % exc, file=sys.stderr)
                time.sleep(1)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self


def coalesce(messages):
    """Split one XREADGROUP batch into (messages to process, IDs to ack unprocessed).

    During a replay the ring service republishes a growing ring every few seconds, so a
    batch often holds several versions of one ring. Only the newest is worth a case.
    A message without a ring_id is kept, so it is reported as bad in the usual way.
    """
    newest = {}
    for i, (_msg_id, fields) in enumerate(messages):
        ring_id = (fields or {}).get("ring_id")
        if ring_id is None:
            continue
        held = newest.get(ring_id)
        if held is None or version_key(fields.get("version")) >= version_key(messages[held][1].get("version")):
            newest[ring_id] = i
    keep, skip = [], []
    for i, (msg_id, fields) in enumerate(messages):
        ring_id = (fields or {}).get("ring_id")
        if ring_id is None or newest[ring_id] == i:
            keep.append((msg_id, fields))
        else:
            skip.append(msg_id)
    return keep, skip


def run_redis(use_granite, r=None, max_loops=None, granite_fn=None, worker=None, block_ms=5000,
              mode=None, listener=None, requests_from="$"):
    """The service loop. `mode` is on_demand or all (default: GRANITE_MODE). With a
    `max_loops` (tests) the request stream is polled once per loop instead of from a
    thread, so a test can push a request and see it served; `requests_from` "0" also
    serves requests written before the start (the service itself takes only new ones)."""
    import redis
    r = r or connect_redis()
    try:
        r.xgroup_create(IN_STREAM, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):   # group already exists = fine
            raise
    consumer = socket.gethostname()
    stats = Stats()
    publisher = Publisher(r, stats)
    mode = mode or granite_mode()
    backend = (granite.granite_backend() or ("custom" if granite_fn else "")) if use_granite else ""
    print("case writer listening on '%s' (Granite: %s%s)" % (
        IN_STREAM, backend or "off", (", " + mode) if backend else ""))
    if use_granite and worker is None:
        worker = GraniteWorker(r, stats, granite_fn, publisher)
    if use_granite and listener is None:
        listener = RequestListener(r, publisher, worker, requests_from)
        if max_loops is None:
            listener.start()

    def handle(messages):
        keep, skip = coalesce(messages)
        if skip:
            r.xack(IN_STREAM, GROUP, *skip)   # an older version of a ring in the same batch
        for msg_id, fields in keep:
            if not fields:
                pass   # a pending entry whose message was trimmed away: nothing to do
            else:
                try:
                    # the template first, at once; Granite replaces it when it passes the fact check
                    priority = publisher.template_first(msg_id, fields, use_granite, mode)
                    if priority is not None:
                        worker.submit(fields, msg_id, priority)
                except Exception as exc:
                    print("skipped bad ring message %s: %s" % (msg_id, exc), file=sys.stderr)
            r.xack(IN_STREAM, GROUP, msg_id)

    def read(start, block=None):
        reply = r.xreadgroup(GROUP, consumer, {IN_STREAM: start}, count=10, block=block)
        return [m for _stream, msgs in reply or [] for m in msgs]

    def settle(step, what):
        """Run one Redis step; when Redis does not answer in time (a busy replay on a
        small VM), wait a second and try again instead of exiting. A restart would lose
        the rings in memory and the stats counters."""
        while True:
            try:
                return step()
            except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as exc:
                print("Redis not answering during %s (%s); retrying in 1 s" % (what, exc.__class__.__name__),
                      file=sys.stderr)
                time.sleep(1)

    # Messages this consumer read but never acked (it stopped mid-batch) come back with
    # id "0". Finish those first, then switch to new messages.
    pending = settle(lambda: read("0"), "pending read")
    while pending:
        settle(lambda: handle(pending), "pending")
        pending = settle(lambda: read("0"), "pending read")

    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        messages = settle(lambda: read(">", block_ms), "read")
        settle(lambda: handle(messages), "handle")
        if messages:
            print(stats.line())
        if listener is not None and max_loops is not None:
            listener.poll()
    if worker is not None and max_loops is not None:
        worker.drain()
    return stats


def main():
    granite.load_env_file()
    p = argparse.ArgumentParser(description="Mule Ring Hunter case writer")
    p.add_argument("--file", help="read fake rings from a JSON file and print the cases")
    p.add_argument("--push-fake", help="push fake rings from a JSON file onto Redis")
    p.add_argument("--redis", action="store_true",
                   help="run the real service on Redis (the default when no option is given)")
    p.add_argument("--no-granite", action="store_true", help="always use the template")
    args = p.parse_args()

    use_granite = granite.granite_configured() and not args.no_granite
    if args.file:
        run_file(args.file, use_granite)
    elif args.push_fake:
        push_fake(args.push_fake)
    else:
        # --redis, or no option at all: docker compose runs the module with no arguments.
        run_redis(use_granite)


if __name__ == "__main__":
    main()
