"""
Step 5 - the case writer service.

Reads rings from the Redis stream `rings`, writes cases to the stream `cases`.

How to run (from the repo root):
  python -m services.casewriter.main --file services/casewriter/fake_rings.json       # no Redis: prints cases
  python -m services.casewriter.main --push-fake services/casewriter/fake_rings.json  # put fake rings on Redis
  python -m services.casewriter.main                                                   # the real service (same as --redis)

Redis host and stream names come from common/bus.py (REDIS_HOST, default "redis").

Granite is used automatically when the watsonx settings exist (see granite.py).
Add --no-granite to force the template.
"""
import argparse
import json
import socket
import sys
from datetime import datetime

from common import bus
from services.casewriter.facts import parse_ring, build_facts
from services.casewriter.template import template_summary
from services.casewriter.factcheck import check_reply
from services.casewriter import granite

IN_STREAM = bus.RINGS
OUT_STREAM = bus.CASES
GROUP = "casewriter"


class Stats:
    """Counts how often Granite passes the fact check - a number for the pitch."""
    def __init__(self):
        self.cases = 0
        self.granite_tried = 0
        self.granite_passed = 0

    def line(self):
        return "cases written: %d | Granite passed the fact check in %d of %d tries" % (
            self.cases, self.granite_passed, self.granite_tried)


def write_case(fields, use_granite, stats=None, granite_fn=None):
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
        except Exception as exc:  # network error, bad JSON, timeout...
            note = "granite error: %s" % str(exc)[:200]

    if stats:
        stats.cases += 1
    return {
        "ring_id": ring["ring_id"],
        "version": ring["version"],
        "summary": json.dumps(summary),
        "source": source,
        "check_note": note,
        "written_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "facts": json.dumps(facts),   # extra field: lets the page show the numbers
    }


def granite_case(fields, stats=None, granite_fn=None):
    """Try Granite for one ring. Returns the upgraded case message (source "granite"),
    or None when the call failed or the reply failed the fact check (the reason is printed).

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
    except Exception as exc:  # network error, bad JSON, timeout, model error...
        print("granite error for %s v%s: %s" % (ring["ring_id"], ring["version"], str(exc)[:200]), file=sys.stderr)
        return None
    if not ok:
        print("granite reply rejected for %s v%s: %s" % (ring["ring_id"], ring["version"], why), file=sys.stderr)
        return None
    if stats:
        stats.granite_passed += 1
    return {
        "ring_id": ring["ring_id"],
        "version": ring["version"],
        "summary": json.dumps({k: reply[k] for k in template}),
        "source": "granite",
        "check_note": "passed fact check",
        "written_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "facts": json.dumps(facts),
    }


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


class GraniteWorker:
    """Runs Granite for rings in the background and publishes the upgraded cases.

    The service publishes the template case at once, then hands the ring to this
    worker. Granite on the VM's CPU can take tens of seconds per ring; the page shows
    the template meanwhile and swaps in the Granite summary when it arrives. A ring
    that arrives while the queue is full keeps its template (noted in the log).
    """
    QUEUE_SIZE = 200

    def __init__(self, r, stats, granite_fn=None):
        import queue
        import threading
        self.r, self.stats, self.granite_fn = r, stats, granite_fn
        self.queue = queue.Queue(maxsize=self.QUEUE_SIZE)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, fields):
        try:
            self.queue.put_nowait(fields)
        except Exception:
            print("granite queue full: %s v%s keeps the template" % (fields.get("ring_id"), fields.get("version")),
                  file=sys.stderr)

    def _run(self):
        while True:
            fields = self.queue.get()
            if fields is None:
                return
            try:
                case = granite_case(fields, self.stats, self.granite_fn)
                if case:
                    self.r.xadd(OUT_STREAM, case)
                    print("case for %s v%s (granite, replaced the template)" % (case["ring_id"], case["version"]))
            except Exception as exc:
                print("granite worker error: %s" % exc, file=sys.stderr)
            finally:
                self.queue.task_done()

    def drain(self):
        """Wait for every queued ring (tests and shutdown)."""
        self.queue.join()


def run_redis(use_granite, r=None, max_loops=None, granite_fn=None, worker=None):
    import redis
    r = r or connect_redis()
    try:
        r.xgroup_create(IN_STREAM, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):   # group already exists = fine
            raise
    consumer = socket.gethostname()
    stats = Stats()
    backend = (granite.granite_backend() or ("custom" if granite_fn else "")) if use_granite else ""
    print("case writer listening on '%s' (Granite: %s)" % (IN_STREAM, backend or "off"))
    if use_granite and worker is None:
        worker = GraniteWorker(r, stats, granite_fn)
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        reply = r.xreadgroup(GROUP, consumer, {IN_STREAM: ">"}, count=10, block=5000)
        for _stream, messages in reply or []:
            for msg_id, fields in messages:
                try:
                    # the template first, at once; Granite replaces it when it passes the fact check
                    case = write_case(fields, False, stats)
                    r.xadd(OUT_STREAM, case)
                    print("case for %s v%s (%s)" % (case["ring_id"], case["version"], case["source"]))
                    if use_granite:
                        worker.submit(fields)
                except Exception as exc:
                    print("skipped bad ring message %s: %s" % (msg_id, exc), file=sys.stderr)
                r.xack(IN_STREAM, GROUP, msg_id)
        if stats.cases:
            print(stats.line())
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
