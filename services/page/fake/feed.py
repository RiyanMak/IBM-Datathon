"""Fake feed: fills Redis so the investigator page can be built and looked at alone.

Everything goes onto the real stream names from common/bus.py with the field sets in
the team guide's Handoffs table, so the page cannot tell these messages from real ones.
Every fake value is visibly fake: ring IDs R-0001..R-0003 (the case writer's
fake_rings.json) or R-9xxxx (patterns_to_rings.py), scored txn_id values starting FAKE-, decision notes starting FAKE.

Run from the repo root, with a local Redis (REDIS_HOST=localhost):

  python -m services.page.fake.feed                    # 3 rings, their template cases, 300 scored rows
  python -m services.page.fake.feed --decisions        # also 2 decisions, to see the badges
  python -m services.page.fake.feed --fake-upgrade 10  # 10 s later, relabel one case as granite
  python -m services.page.fake.feed --live             # keep adding scored rows so counters move
  python -m services.page.fake.feed --bump R-0001      # re-send one ring: version + 1, one more transfer
  python -m services.page.fake.feed --reset            # delete the 5 streams first (fake data only)

Delete this folder once the real stack runs end to end.
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

from common import bus
from services.casewriter.casewriter import (
    CASE_KEY, push_fake, ring_to_stream_fields, store_case, write_case,
)

HERE = os.path.dirname(os.path.abspath(__file__))
RINGS_FILE = os.path.join(HERE, "..", "..", "casewriter", "fake_rings.json")

FAKE_TXN_PREFIX = "FAKE-"
# R-000x: fake_rings.json. R-9xxxx: patterns_to_rings.py (real attempts, still test input).
FAKE_RING_PREFIXES = ("R-000", "R-9")
FAKE_NOTE_PREFIX = "FAKE"

# Data time for fake transfers: the same days as the fake rings.
DATA_START = datetime(2022, 9, 7, 0, 0, 0)
DATA_SPAN_S = 3 * 24 * 3600

REASON_PHRASES = [
    "money left {m} minutes after it arrived",
    "{n} payees never paid before",
    "{p}% passed straight on",
    "{k} transfers in the past hour",
    "amount is {x} times the sender's usual",
    "sender account is {d} days old",
]

CURRENCIES = ["US Dollar", "Euro", "UK Pound", "Yen"]


def utc_now():
    return datetime.now(timezone.utc)


def iso_wall(dt):
    """Wall-clock time, UTC, milliseconds: what the scorer and the page stamp."""
    return dt.isoformat(timespec="milliseconds")


def fake_account(rng):
    return "%d_FAKE%05X" % (rng.randint(1, 300), rng.randint(0, 0xFFFFF))


def fake_reasons(rng):
    picks = rng.sample(REASON_PHRASES, 3)
    values = {"m": rng.randint(5, 90), "n": rng.randint(2, 9), "p": rng.randint(80, 99),
              "k": rng.randint(3, 15), "x": rng.randint(3, 20), "d": rng.randint(1, 30)}
    return [p.format(**values) for p in picks]


def fake_scored_row(rng, seq):
    """One message for the `scored` stream: the txns fields plus the scorer's fields."""
    amount = round(rng.lognormvariate(7, 1.4), 2)
    ccy = rng.choice(CURRENCIES)
    score = round(rng.betavariate(0.6, 6), 4)
    flagged = 1 if score >= 0.5 else 0
    sent = utc_now() - timedelta(milliseconds=rng.randint(5, 60))
    latency_ms = rng.lognormvariate(1.6, 0.6)          # a few ms, occasional slow tail
    scored = sent + timedelta(milliseconds=latency_ms)
    ts = DATA_START + timedelta(seconds=rng.randint(0, DATA_SPAN_S))
    clues = {
        "snd_age_days": rng.randint(0, 400),
        "mins_since_snd_last_in": rng.randint(-1, 1440),
        "snd_payees_24h": rng.randint(0, 12),
        "new_payee": rng.randint(0, 1),
        "pass_through": round(rng.random(), 3),
        "rcv_payers_24h": rng.randint(0, 12),
        "snd_out_count_1h": rng.randint(0, 15),
        "amt_vs_snd_avg": round(rng.lognormvariate(0, 0.8), 3),
    }
    return {
        "txn_id": "%s%06d" % (FAKE_TXN_PREFIX, seq),
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "from_acct": fake_account(rng),
        "to_acct": fake_account(rng),
        "amount_usd": str(amount),
        "amt_paid": str(amount),
        "ccy_paid": ccy,
        "amt_recv": str(amount),
        "ccy_recv": ccy,
        "fmt": rng.choice(["ACH", "Wire", "Cheque", "Credit Card"]),
        "sent_at": iso_wall(sent),
        "score": str(score),
        "flagged": str(flagged),
        "reasons": json.dumps(fake_reasons(rng) if flagged else []),
        "clues": json.dumps(clues),
        "scored_at": iso_wall(scored),
    }


def next_seq(r):
    """Continue FAKE- numbering after whatever is already on `scored`."""
    last = r.xrevrange(bus.SCORED, "+", "-", count=1)
    if last:
        txn = last[0][1].get("txn_id", "")
        if txn.startswith(FAKE_TXN_PREFIX):
            try:
                return int(txn[len(FAKE_TXN_PREFIX):]) + 1
            except ValueError:
                pass
    return 1


def push_scored(r, rng, n):
    start = next_seq(r)
    pipe = r.pipeline(transaction=False)
    for i in range(n):
        pipe.xadd(bus.SCORED, fake_scored_row(rng, start + i))
    pipe.execute()
    return n


def load_rings(path):
    with open(path) as fh:
        return json.load(fh)


def push_cases(r, rings):
    """Template cases, made and stored by the real case writer code (no Granite): on the
    `cases` stream and in the `case:<ring_id>` hash, exactly as the service does."""
    for ring in rings:
        store_case(r, write_case(ring_to_stream_fields(ring), use_granite=False))
    return len(rings)


def newest_ring(r, ring_id, rings):
    """The newest copy of a fake ring: from the `rings` stream if it is there, else the file."""
    if not str(ring_id).startswith(FAKE_RING_PREFIXES):
        raise ValueError("%s is not a fake ring ID" % ring_id)   # never touch a real ring
    best = None
    for _id, fields in r.xrevrange(bus.RINGS, "+", "-", count=1000):
        if fields.get("ring_id") != ring_id:
            continue
        version = int(float(fields.get("version") or 0))
        if best is None or version > best["version"]:
            best = {k: (json.loads(v) if k in ("accounts", "edges", "reasons") and v else v)
                    for k, v in fields.items()}
            best["version"] = version
    if best is None:
        best = next((dict(ring) for ring in rings if ring["ring_id"] == ring_id), None)
    if best is None:
        raise ValueError("%s is not a fake ring in the rings file or on '%s'" % (ring_id, bus.RINGS))
    return best


def bump_ring(r, ring_id, rings):
    """Re-send a fake ring with version + 1 and one more transfer, plus its template case.

    The extra transfer repeats the ring's last link ten data-minutes later, so the page's
    "latest version wins" path can be watched without a real ring service.
    """
    ring = newest_ring(r, ring_id, rings)
    last = ring["edges"][-1]
    as_list = isinstance(last, (list, tuple))
    src, dst, ts = (last[1], last[2], last[4]) if as_list else (last["from"], last["to"], last["ts"])
    ring["version"] = int(ring["version"]) + 1
    new_ts = (datetime.fromisoformat(str(ts)) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
    txn_id = "%sBUMP-%s-%d" % (FAKE_TXN_PREFIX, ring_id, ring["version"])
    edge = [txn_id, src, dst, 250.0, new_ts, 0.6]
    ring["edges"] = list(ring["edges"]) + [edge if as_list else dict(zip(
        ("txn_id", "from", "to", "amount_usd", "ts", "score"), edge))]
    ring["last_seen"] = new_ts
    fields = ring_to_stream_fields(ring)
    r.xadd(bus.RINGS, fields)
    store_case(r, write_case(fields, use_granite=False))
    return ring


def push_fake_upgrade(r, ring):
    """Re-send a ring's template case labelled as granite, to see the upgrade moment.

    The text is still the template's. check_note says so, so it can never pass as a
    real Granite result in a screenshot.
    """
    case = write_case(ring_to_stream_fields(ring), use_granite=False)
    case["source"] = "granite"
    case["check_note"] = "%s upgrade: template text relabelled, no Granite call" % FAKE_NOTE_PREFIX
    r.xadd(bus.CASES, case)


def push_decisions(r, rings):
    plan = [("more_info", 1), ("freeze", 2)]   # (decision, index into rings)
    n = 0
    for decision, idx in plan:
        if idx >= len(rings):
            continue
        ring = rings[idx]
        r.xadd(bus.DECISIONS, {
            "ring_id": ring["ring_id"],
            "version": str(ring["version"]),
            "decision": decision,
            "note": "%s decision for testing badges" % FAKE_NOTE_PREFIX,
            "decided_at": iso_wall(utc_now()),
        })
        n += 1
    return n


def find_real_data(r, sample=1000):
    """Return a reason string if any stream holds something that is not fake, else None."""
    if r.exists(bus.TXNS) and r.xlen(bus.TXNS):
        return "'%s' has messages (a real replayer ran)" % bus.TXNS
    checks = [
        (bus.SCORED, "txn_id", FAKE_TXN_PREFIX),
        (bus.RINGS, "ring_id", FAKE_RING_PREFIXES),
        (bus.CASES, "ring_id", FAKE_RING_PREFIXES),
        # By ring, not by note: decisions clicked on the page for a fake ring are test data too.
        (bus.DECISIONS, "ring_id", FAKE_RING_PREFIXES),
    ]
    for stream, field, prefix in checks:
        for _id, fields in r.xrevrange(stream, "+", "-", count=sample):
            if not fields.get(field, "").startswith(prefix):
                return "'%s' has a non-fake message (%s=%r)" % (stream, field, fields.get(field))
    return None


def reset(r, force=False):
    real = find_real_data(r)
    if real and not force:
        print("refusing to reset: %s. Use --force if you are sure." % real, file=sys.stderr)
        return False
    r.delete(*bus.STREAMS)
    r.delete("page:feed_mode")
    # the latest-case hashes of fake rings, or the page would keep showing them
    stale = [k for k in r.scan_iter(CASE_KEY % "*") if k[len(CASE_KEY % ""):].startswith(FAKE_RING_PREFIXES)]
    if stale:
        r.delete(*stale)
    print("deleted streams: %s" % ", ".join(bus.STREAMS))
    return True


def main(argv=None):
    p = argparse.ArgumentParser(description="Fake feed for the investigator page")
    p.add_argument("--rings", default=RINGS_FILE, help="rings JSON file (default: case writer's fake_rings.json)")
    p.add_argument("--scored", type=int, default=300, help="fake scored rows to push (default 300)")
    p.add_argument("--decisions", action="store_true", help="also push 2 fake decisions")
    p.add_argument("--fake-upgrade", type=float, metavar="SECONDS",
                   help="after SECONDS, relabel the first ring's case as granite")
    p.add_argument("--live", action="store_true", help="keep pushing scored rows until Ctrl+C")
    p.add_argument("--rate", type=int, default=20, help="scored rows per second with --live (default 20)")
    p.add_argument("--reset", action="store_true", help="delete the 5 streams first (refuses on real data)")
    p.add_argument("--force", action="store_true", help="with --reset, delete even if real data is found")
    p.add_argument("--seed", type=int, default=None, help="random seed, for repeatable rows")
    p.add_argument("--bump", metavar="RING_ID",
                   help="only re-send this fake ring with version + 1 and one more transfer, then stop")
    args = p.parse_args(argv)

    r = bus.connect()
    rng = random.Random(args.seed)
    print("Redis at %s:%d" % (bus.redis_host(), bus.redis_port()))

    if args.bump:
        try:
            ring = bump_ring(r, args.bump, load_rings(args.rings))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("pushed %s version %d (%d transfers) and its template case" % (
            ring["ring_id"], ring["version"], len(ring["edges"])))
        return 0

    if args.reset and not reset(r, args.force):
        return 1

    rings = load_rings(args.rings)
    r.set("page:feed_mode", "fixture")
    push_fake(args.rings, r)
    print("pushed %d template cases onto '%s'" % (push_cases(r, rings), bus.CASES))
    print("pushed %d fake scored rows onto '%s'" % (push_scored(r, rng, args.scored), bus.SCORED))
    if args.decisions:
        print("pushed %d fake decisions onto '%s'" % (push_decisions(r, rings), bus.DECISIONS))

    if args.fake_upgrade is not None and rings:
        print("waiting %.0f s before the fake granite upgrade of %s ..." % (args.fake_upgrade, rings[0]["ring_id"]))
        time.sleep(args.fake_upgrade)
        push_fake_upgrade(r, rings[0])
        print("pushed fake granite upgrade for %s" % rings[0]["ring_id"])

    if args.live:
        print("live: %d scored rows per second, Ctrl+C to stop" % args.rate)
        try:
            while True:
                push_scored(r, rng, args.rate)
                time.sleep(1)
        except KeyboardInterrupt:
            print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
