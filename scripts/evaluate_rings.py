"""Ring-level evaluation: how many known laundering attempts did our rings catch, how fast,
and how much of the money was still stoppable?

Compares the rings the ring service published (the `rings` stream, or a JSON-lines dump
of it) with the known attempts in HI-Small_Patterns.txt. Only attempts that start in the
replayed period count (default: from the test start in splits.json).

Usage (from the repo root):
  python -m scripts.evaluate_rings --patterns ~/Downloads/HI-Small_Patterns.txt \
      --rates data/processed/fx_rates.json --splits data/processed/splits.json --redis
  python -m scripts.evaluate_rings --patterns ... --rings-file rings.jsonl --out eval.json

Definitions (from the team guide):
  caught          a ring catches an attempt when at least half of the ring's accounts belong
                  to the attempt. We also report the softer "covered": at least half of the
                  attempt's accounts are in one ring.
  time to alert   minutes from the attempt's first transfer to the data time at which the
                  first catching ring version was published (published_at; falls back to the
                  ring's last_seen for messages without it)
  still stoppable share of the attempt's money (USD) that moved after that alert time
  shape match     the catching ring's shape equals the attempt's shape name

The labels in HI-Small_Trans.csv include laundering transfers that belong to no attempt
in the patterns file, so a ring that matches no attempt is not necessarily wrong.
"""

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

from common.data_contract import make_account_id
from services.casewriter.patterns_to_rings import load_rates, read_attempts

RAW_TS = "%Y/%m/%d %H:%M"
ISO = "%Y-%m-%dT%H:%M:%S"
SHAPE_NAMES = {"fan-out": "fan-out", "fan-in": "fan-in", "cycle": "cycle", "scatter-gather": "scatter-gather",
               "gather-scatter": "gather-scatter", "bipartite": "bipartite", "stack": "stack", "random": "other"}


def normalise_shape(raw: str) -> str:
    key = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    return SHAPE_NAMES.get(key, key or "other")


# ---------------------------------------------------------------- attempts

def load_attempts(patterns_path, rates) -> list:
    """Every attempt in the patterns file as a dict: shape, accounts, transfers, first/last time, total USD."""
    attempts = []
    for index, (shape, rows) in enumerate(read_attempts(patterns_path), start=1):
        transfers, money_known = [], True
        for r in rows:
            if len(r) < 11:
                continue
            ts = datetime.strptime(r[0].strip(), RAW_TS)
            src, dst = make_account_id(r[1], r[2].strip()), make_account_id(r[3], r[4].strip())
            amount, currency = float(r[7]), r[8].strip()
            if currency == "US Dollar":
                usd = amount
            elif currency in rates:
                usd = amount * rates[currency]
            else:
                usd, money_known = 0.0, False
            transfers.append({"ts": ts, "from": src, "to": dst, "usd": usd})
        if not transfers:
            continue
        transfers.sort(key=lambda t: t["ts"])
        accounts = {t["from"] for t in transfers} | {t["to"] for t in transfers}
        attempts.append({
            "id": "A-%04d" % index,
            "shape": normalise_shape(shape),
            "accounts": accounts,
            "transfers": transfers,
            "first_ts": transfers[0]["ts"],
            "last_ts": transfers[-1]["ts"],
            "total_usd": sum(t["usd"] for t in transfers),
            "money_known": money_known,
        })
    return attempts


# ---------------------------------------------------------------- rings

def parse_ring_message(fields) -> dict:
    """One `rings` message (text fields) -> what the evaluation needs."""
    accounts = json.loads(fields.get("accounts") or "[]")
    published = fields.get("published_at") or fields.get("last_seen") or ""
    return {
        "ring_id": fields["ring_id"],
        "version": int(fields.get("version") or 1),
        "shape": normalise_shape(fields.get("shape", "")),
        "accounts": set(accounts),
        "published_at": datetime.strptime(published, ISO) if published else None,
    }


def rings_from_redis(r, stream="rings") -> list:
    return [parse_ring_message(f) for _, f in r.xrange(stream, "-", "+")]


def rings_from_file(path) -> list:
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(parse_ring_message(json.loads(line)))
    return out


# ---------------------------------------------------------------- evaluation

def evaluate(attempts, ring_versions, since=None, until=None) -> dict:
    """Match every attempt in [since, until] against every ring version. Returns the summary."""
    in_period = [a for a in attempts
                 if (since is None or a["first_ts"] >= since) and (until is None or a["first_ts"] <= until)]
    by_account = defaultdict(set)              # account -> attempt indexes, to find candidates fast
    for i, a in enumerate(in_period):
        for acct in a["accounts"]:
            by_account[acct].add(i)

    results = [{"attempt": a, "caught_by": None, "alert_ts": None, "covered": False, "shape_match": False,
                "shape_match_final": False} for a in in_period]
    rings_with_attempt = set()
    latest_shape = {}                           # ring_id -> shape of its latest version
    for ring in ring_versions:
        if ring["version"] >= latest_shape.get(ring["ring_id"], (0, ""))[0]:
            latest_shape[ring["ring_id"]] = (ring["version"], ring["shape"])
    for ring in sorted(ring_versions, key=lambda x: (x["published_at"] or datetime.max, x["version"])):
        candidates = set()
        for acct in ring["accounts"]:
            candidates |= by_account.get(acct, set())
        for i in candidates:
            a = in_period[i]
            shared = len(ring["accounts"] & a["accounts"])
            if shared >= len(a["accounts"]) / 2:
                results[i]["covered"] = True
            if shared >= len(ring["accounts"]) / 2:
                rings_with_attempt.add(ring["ring_id"])
                res = results[i]
                if res["caught_by"] is None:        # the first version that catches it
                    res["caught_by"] = ring["ring_id"]
                    res["alert_ts"] = ring["published_at"]
                    res["shape_match"] = ring["shape"] == a["shape"]
                    res["shape_match_final"] = latest_shape[ring["ring_id"]][1] == a["shape"]

    caught = [r for r in results if r["caught_by"]]
    minutes, stoppable = [], []
    for r in caught:
        a = r["attempt"]
        if r["alert_ts"] is not None:
            minutes.append((r["alert_ts"] - a["first_ts"]).total_seconds() / 60.0)
            if a["money_known"] and a["total_usd"] > 0:
                after = sum(t["usd"] for t in a["transfers"] if t["ts"] > r["alert_ts"])
                stoppable.append(after / a["total_usd"])

    per_shape = {}
    for shape in sorted({a["shape"] for a in in_period}):
        rs = [r for r in results if r["attempt"]["shape"] == shape]
        c = [r for r in rs if r["caught_by"]]
        per_shape[shape] = {
            "attempts": len(rs), "caught": len(c),
            "shape_matched": sum(1 for r in c if r["shape_match"]),
            "shape_matched_final": sum(1 for r in c if r["shape_match_final"]),
        }

    distinct_rings = {x["ring_id"] for x in ring_versions}
    return {
        "attempts_in_period": len(in_period),
        "caught": len(caught),
        "caught_share": len(caught) / len(in_period) if in_period else None,
        "covered": sum(1 for r in results if r["covered"]),
        "shape_matched": sum(1 for r in caught if r["shape_match"]),
        "shape_match_share": (sum(1 for r in caught if r["shape_match"]) / len(caught)) if caught else None,
        "shape_matched_final": sum(1 for r in caught if r["shape_match_final"]),
        "shape_match_final_share": (sum(1 for r in caught if r["shape_match_final"]) / len(caught)) if caught else None,
        "minutes_to_alert_median": statistics.median(minutes) if minutes else None,
        "minutes_to_alert_mean": statistics.fmean(minutes) if minutes else None,
        "still_stoppable_median": statistics.median(stoppable) if stoppable else None,
        "still_stoppable_mean": statistics.fmean(stoppable) if stoppable else None,
        "rings_total": len(distinct_rings),
        "rings_matching_an_attempt": len(rings_with_attempt),
        "per_shape": per_shape,
        "caught_attempts": [{"attempt": r["attempt"]["id"], "shape": r["attempt"]["shape"],
                             "ring": r["caught_by"], "shape_match": r["shape_match"],
                             "shape_match_final": r["shape_match_final"],
                             "minutes_to_alert": ((r["alert_ts"] - r["attempt"]["first_ts"]).total_seconds() / 60.0)
                             if r["alert_ts"] else None}
                            for r in caught],
    }


def pct(x):
    return "-" if x is None else "%.0f%%" % (100 * x)


def num(x, fmt="%.0f"):
    return "-" if x is None else fmt % x


def report(summary) -> str:
    s = summary
    lines = [
        "| Measure | Value |", "|---|---|",
        "| Known attempts starting in the period | %d |" % s["attempts_in_period"],
        "| Caught by a ring (half the ring's accounts belong to the attempt) | %d (%s) |" % (s["caught"], pct(s["caught_share"])),
        "| Covered (half the attempt's accounts in one ring) | %d |" % s["covered"],
        "| Shape name matches at the first alert, among caught | %d (%s) |" % (s["shape_matched"], pct(s["shape_match_share"])),
        "| Shape name matches in the ring's latest version | %d (%s) |" % (s["shape_matched_final"], pct(s["shape_match_final_share"])),
        "| Minutes from first transfer to alert, median / mean | %s / %s |" % (
            num(s["minutes_to_alert_median"]), num(s["minutes_to_alert_mean"])),
        "| Money still stoppable at the alert, median / mean | %s / %s |" % (
            pct(s["still_stoppable_median"]), pct(s["still_stoppable_mean"])),
        "| Rings published (distinct ids) | %d |" % s["rings_total"],
        "| Rings matching a known attempt | %d |" % s["rings_matching_an_attempt"],
        "", "| Shape | Attempts | Caught | Shape matched at alert | Shape matched, latest |", "|---|---|---|---|---|",
    ]
    for shape, v in s["per_shape"].items():
        lines.append("| %s | %d | %d | %d | %d |" % (shape, v["attempts"], v["caught"], v["shape_matched"],
                                                   v["shape_matched_final"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- command line

def main(argv=None):
    p = argparse.ArgumentParser(description="Ring-level evaluation against HI-Small_Patterns.txt")
    p.add_argument("--patterns", required=True, help="HI-Small_Patterns.txt")
    p.add_argument("--rates", help="fx_rates.json from scripts/clean_data.py (for the money numbers)")
    p.add_argument("--splits", help="splits.json; attempts before its test_start are ignored")
    p.add_argument("--since", help="count attempts starting at or after this data time, YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--until", help="count attempts starting at or before this data time")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--redis", action="store_true", help="read the rings stream from Redis (REDIS_HOST)")
    src.add_argument("--rings-file", help="JSON lines, one rings message per line")
    p.add_argument("--stream", default="rings")
    p.add_argument("--out", help="write the summary as JSON here")
    args = p.parse_args(argv)

    rates = load_rates(args.rates) if args.rates else {}
    attempts = load_attempts(args.patterns, rates)
    since = until = None
    if args.splits:
        with open(args.splits) as fh:
            since = datetime.strptime(json.load(fh)["test_start"], ISO)
    if args.since:
        since = datetime.strptime(args.since, ISO)
    if args.until:
        until = datetime.strptime(args.until, ISO)

    if args.redis:
        from common import bus
        rings = rings_from_redis(bus.connect(), args.stream)
    else:
        rings = rings_from_file(args.rings_file)

    summary = evaluate(attempts, rings, since, until)
    print("attempts in file: %d | ring versions read: %d | period from %s" % (
        len(attempts), len(rings), since.strftime(ISO) if since else "the beginning"), file=sys.stderr)
    print(report(summary))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(summary, fh, indent=1, default=str)
    return summary


if __name__ == "__main__":
    main()
