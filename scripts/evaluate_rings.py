"""Ring-level evaluation: how many known laundering attempts did our rings catch, how fast,
and how much of the money was still stoppable?

Compares the rings the ring service published (the `rings` stream, or a JSON-lines dump
of it) with the known attempts in HI-Small_Patterns.txt. Only attempts that start in the
replayed period count (default: from the test start in splits.json).

Usage (from the repo root):
  python -m scripts.evaluate_rings --patterns ~/Downloads/HI-Small_Patterns.txt \
      --rates data/processed/fx_rates.json --splits data/processed/splits.json --redis
  python -m scripts.evaluate_rings --labels data/processed/clean.csv --models data/models --redis
  python -m scripts.evaluate_rings --patterns ... --rings-file rings.jsonl --out eval.json

--labels needs no patterns file: it scores the rings against the Is Laundering labels
(share of ring transfers that are laundering, share of all test-period laundering that
sits in a ring, precision of core versus expanded transfers).

--gnn-sweep measures what the GNN adds. Replay once with the ring service publishing
every ring (no GNN threshold), then the sweep keeps, for each threshold, only the ring
versions whose gnn_score is at or above it (exactly what --gnn-threshold in the ring
service would publish) and reports the numbers again. The "no GNN" row is the baseline:
  python -m scripts.evaluate_rings --labels data/processed/clean.csv --patterns ... --redis --gnn-sweep

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
from pathlib import Path

from common.data_contract import make_account_id
from services.casewriter.patterns_to_rings import load_rates, read_attempts

RAW_TS = "%Y/%m/%d %H:%M"
ISO = "%Y-%m-%dT%H:%M:%S"
GNN_SWEEP = (0.1, 0.3, 0.5, 0.7, 0.9)
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
        "gnn_score": gnn_score_of(fields),
    }


def gnn_score_of(fields):
    """The ring's gnn_score as a float, or None when the ring service sent it empty."""
    raw = fields.get("gnn_score")
    return float(raw) if raw not in (None, "") else None


def passes_gnn(fields, threshold) -> bool:
    """Would the ring service publish this version with --gnn-threshold threshold?
    The same rule as RingService.gnn_dropped: an empty gnn_score is never held back."""
    score = gnn_score_of(fields)
    return threshold is None or score is None or score >= threshold


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

def match_rings(attempts, ring_versions):
    """For every attempt, the first ring version that catches it (half the ring's accounts
    belong to the attempt) and whether some ring covers it (half the attempt's accounts in
    one ring). Returns (one result per attempt, ids of rings that caught any attempt)."""
    by_account = defaultdict(set)              # account -> attempt indexes, to find candidates fast
    for i, a in enumerate(attempts):
        for acct in a["accounts"]:
            by_account[acct].add(i)

    results = [{"attempt": a, "caught_by": None, "alert_ts": None, "covered": False, "shape_match": False,
                "shape_match_final": False} for a in attempts]
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
            a = attempts[i]
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
    return results, rings_with_attempt


def minutes_to_alert(result):
    """Data minutes from the attempt's first transfer to the alert; None without an alert time.
    A ring that formed from other activity can alert before the attempt's first transfer;
    that counts as 0 minutes (the person could act at once), and the caller counts how often."""
    if result["alert_ts"] is None:
        return None
    return max(0.0, (result["alert_ts"] - result["attempt"]["first_ts"]).total_seconds() / 60.0)


def stoppable_share(result):
    """Share of the attempt's money that moved after the alert; None when the money is unknown."""
    a = result["attempt"]
    if result["alert_ts"] is None or not a["money_known"] or a["total_usd"] <= 0:
        return None
    after = sum(t["usd"] for t in a["transfers"] if t["ts"] > result["alert_ts"])
    return after / a["total_usd"]


def evaluate(attempts, ring_versions, since=None, until=None) -> dict:
    """Match every attempt that starts in [since, until] against every ring version.

    Attempts that started before `since` but were still running at it (the replay only
    shows their tail) are matched too and reported separately under "started_earlier";
    they are never part of the headline denominator.
    """
    in_period = [a for a in attempts
                 if (since is None or a["first_ts"] >= since) and (until is None or a["first_ts"] <= until)]
    earlier = [a for a in attempts if since is not None and a["first_ts"] < since <= a["last_ts"]]
    results, rings_with_attempt = match_rings(in_period, ring_versions)
    earlier_results, earlier_rings = match_rings(earlier, ring_versions)
    rings_with_attempt |= earlier_rings

    caught = [r for r in results if r["caught_by"]]
    minutes, stoppable = [], []
    alerts_before_first = 0
    stoppable_usd = attempts_usd = 0.0
    for r in caught:
        if r["alert_ts"] is None:
            continue
        if r["alert_ts"] < r["attempt"]["first_ts"]:
            alerts_before_first += 1
        minutes.append(minutes_to_alert(r))
        share = stoppable_share(r)
        if share is not None:
            stoppable.append(share)
            stoppable_usd += share * r["attempt"]["total_usd"]
            attempts_usd += r["attempt"]["total_usd"]

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
        "minutes_to_alert_p90": percentile(minutes, 90),
        "alerts_before_first_transfer": alerts_before_first,
        "still_stoppable_median": statistics.median(stoppable) if stoppable else None,
        "still_stoppable_mean": statistics.fmean(stoppable) if stoppable else None,
        "still_stoppable_usd": stoppable_usd,
        "caught_attempts_usd": attempts_usd,
        "rings_total": len(distinct_rings),
        "rings_matching_an_attempt": len(rings_with_attempt),
        "started_earlier": {
            "attempts": len(earlier),
            "caught": sum(1 for r in earlier_results if r["caught_by"]),
            "covered": sum(1 for r in earlier_results if r["covered"]),
        },
        "per_shape": per_shape,
        "caught_attempts": [{"attempt": r["attempt"]["id"], "shape": r["attempt"]["shape"],
                             "ring": r["caught_by"], "shape_match": r["shape_match"],
                             "shape_match_final": r["shape_match_final"],
                             "first_ts": r["attempt"]["first_ts"].strftime(ISO),
                             "alert_ts": r["alert_ts"].strftime(ISO) if r["alert_ts"] else None,
                             "minutes_to_alert": minutes_to_alert(r),
                             "stoppable_share": stoppable_share(r),
                             "total_usd": r["attempt"]["total_usd"] if r["attempt"]["money_known"] else None}
                            for r in caught],
        "missed_attempts": [{"attempt": r["attempt"]["id"], "shape": r["attempt"]["shape"],
                             "accounts": len(r["attempt"]["accounts"]), "covered": r["covered"]}
                            for r in results if not r["caught_by"]],
    }


def percentile(values, pct):
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(-(-pct * len(ordered) // 100)))   # ceil without floats
    return ordered[min(rank, len(ordered)) - 1]


def load_labels(clean_csv):
    """(txn_id -> label, number of laundering transfers) for the test period of clean.csv."""
    import pandas as pd

    labels = pd.read_csv(clean_csv, usecols=["txn_id", "label", "split", "ts"], dtype={"txn_id": str, "label": "int8"})
    period = labels[labels.split == "test"] if "test" in set(labels.split) else labels
    return dict(zip(period.txn_id, period.label)), int(period.label.sum())


def label_summary(ring_messages, clean_csv=None, ring_threshold=None, labels=None) -> dict:
    """Ring quality against the transfer labels in clean.csv (available without the patterns file).

    Uses the latest version of every ring. Reports the share of ring transfers labelled
    laundering, the share of rings that are mostly laundering, the share of all laundering
    transfers in the period that sit in some ring, and, when ring_threshold is given, the
    precision of core transfers (score >= ring_threshold) versus expanded ones.
    labels: the result of load_labels, to read clean.csv only once for several summaries.
    """
    latest = {}
    for f in ring_messages:
        if int(f.get("version") or 1) >= int(latest.get(f["ring_id"], {}).get("version") or 0):
            latest[f["ring_id"]] = f
    label_of, n_laundering = labels if labels is not None else load_labels(clean_csv)

    kinds = {"core": [0, 0], "expanded": [0, 0]}
    rings, in_ring = [], set()
    for f in latest.values():
        edges = json.loads(f.get("edges") or "[]")
        hits = 0
        for e in edges:
            y = int(label_of.get(str(e["txn_id"]), 0))
            hits += y
            if y:
                in_ring.add(str(e["txn_id"]))
            if ring_threshold is not None:
                kind = "core" if float(e.get("score") or 0) >= ring_threshold else "expanded"
                kinds[kind][0] += y
                kinds[kind][1] += 1
        rings.append({"ring_id": f["ring_id"], "shape": f.get("shape", ""), "transfers": len(edges),
                      "laundering": hits, "precision": hits / len(edges) if edges else 0.0})
    n_transfers = sum(r["transfers"] for r in rings)
    n_hits = sum(r["laundering"] for r in rings)
    by_shape = {}
    for r in rings:
        s = by_shape.setdefault(r["shape"], {"rings": 0, "precision_sum": 0.0})
        s["rings"] += 1
        s["precision_sum"] += r["precision"]
    return {
        "rings": len(rings),
        "transfers_in_rings": n_transfers,
        "laundering_in_rings": n_hits,
        "ring_transfer_precision": n_hits / n_transfers if n_transfers else None,
        "rings_mostly_laundering": sum(1 for r in rings if r["precision"] >= 0.5),
        "rings_without_laundering": sum(1 for r in rings if r["laundering"] == 0),
        "laundering_in_period": n_laundering,
        "laundering_covered": len(in_ring),
        "laundering_covered_share": len(in_ring) / n_laundering if n_laundering else None,
        "core_precision": kinds["core"][0] / kinds["core"][1] if kinds["core"][1] else None,
        "expanded_precision": kinds["expanded"][0] / kinds["expanded"][1] if kinds["expanded"][1] else None,
        "expanded_transfers": kinds["expanded"][1],
        "per_shape": {k: {"rings": v["rings"], "mean_precision": v["precision_sum"] / v["rings"]}
                      for k, v in sorted(by_shape.items())},
    }


def gnn_sweep(raw, thresholds=GNN_SWEEP, attempts=None, since=None, until=None, labels=None,
              ring_threshold=None) -> list:
    """The evaluation repeated with only the ring versions the GNN would let through.

    raw: the rings messages as published with no GNN threshold. One row per threshold,
    the first (threshold None) is the baseline without the GNN. Attempt numbers need
    attempts (load_attempts), label numbers need labels (load_labels).
    """
    rows = []
    for t in (None, *thresholds):
        kept = [f for f in raw if passes_gnn(f, t)]
        row = {"gnn_threshold": t, "ring_versions": len(kept), "rings": len({f["ring_id"] for f in kept})}
        if attempts is not None:
            s = evaluate(attempts, [parse_ring_message(f) for f in kept], since, until)
            row.update(attempts_caught=s["caught"], caught_share=s["caught_share"],
                       rings_matching_an_attempt=s["rings_matching_an_attempt"],
                       ring_attempt_precision=s["rings_matching_an_attempt"] / s["rings_total"] if s["rings_total"] else None,
                       minutes_to_alert_median=s["minutes_to_alert_median"])
        if labels is not None:
            s = label_summary(kept, ring_threshold=ring_threshold, labels=labels)
            row.update(ring_transfer_precision=s["ring_transfer_precision"],
                       rings_mostly_laundering=s["rings_mostly_laundering"],
                       rings_mostly_laundering_share=s["rings_mostly_laundering"] / s["rings"] if s["rings"] else None,
                       rings_without_laundering=s["rings_without_laundering"],
                       laundering_covered_share=s["laundering_covered_share"])
        rows.append(row)
    return rows


def sweep_report(rows) -> str:
    has_attempts = "attempts_caught" in rows[0]
    has_labels = "ring_transfer_precision" in rows[0]
    head = ["GNN threshold", "Rings"]
    if has_attempts:
        head += ["Attempts caught", "Rings matching an attempt", "Minutes to alert (median)"]
    if has_labels:
        head += ["Rings mostly laundering", "Rings with no laundering", "Ring transfers laundering",
                 "Laundering in a ring"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        cells = ["no GNN" if r["gnn_threshold"] is None else "%.2f" % r["gnn_threshold"], "%d" % r["rings"]]
        if has_attempts:
            cells += ["%d (%s)" % (r["attempts_caught"], pct(r["caught_share"])),
                      "%d (%s)" % (r["rings_matching_an_attempt"], pct(r["ring_attempt_precision"])),
                      num(r["minutes_to_alert_median"])]
        if has_labels:
            cells += ["%d (%s)" % (r["rings_mostly_laundering"], pct(r["rings_mostly_laundering_share"])),
                      "%d" % r["rings_without_laundering"], pct(r["ring_transfer_precision"]),
                      pct(r["laundering_covered_share"])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def label_report(s) -> str:
    lines = [
        "| Against the transfer labels | Value |", "|---|---|",
        "| Rings (latest versions) | %d |" % s["rings"],
        "| Transfers in rings, labelled laundering | %d of %d (%s) |" % (
            s["laundering_in_rings"], s["transfers_in_rings"], pct(s["ring_transfer_precision"])),
        "| Rings that are mostly laundering | %d (%d with none) |" % (
            s["rings_mostly_laundering"], s["rings_without_laundering"]),
        "| Laundering transfers in the period that sit in a ring | %d of %d (%s) |" % (
            s["laundering_covered"], s["laundering_in_period"], pct(s["laundering_covered_share"])),
    ]
    if s["core_precision"] is not None:
        lines.append("| Precision of core transfers / expanded transfers | %s / %s (%d expanded) |" % (
            pct(s["core_precision"]), pct(s["expanded_precision"]), s["expanded_transfers"]))
    lines += ["", "| Shape | Rings | Mean precision |", "|---|---|---|"]
    for shape, v in s["per_shape"].items():
        lines.append("| %s | %d | %s |" % (shape, v["rings"], pct(v["mean_precision"])))
    return "\n".join(lines)


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
        "| Minutes from first transfer to alert, median / mean / 90th percentile | %s / %s / %s |" % (
            num(s["minutes_to_alert_median"]), num(s["minutes_to_alert_mean"]), num(s.get("minutes_to_alert_p90"))),
        "| Money still stoppable at the alert, median / mean | %s / %s |" % (
            pct(s["still_stoppable_median"]), pct(s["still_stoppable_mean"])),
        "| Money still stoppable, total over caught attempts | %s of %s |" % (
            usd(s.get("still_stoppable_usd")), usd(s.get("caught_attempts_usd"))),
        "| Rings published (distinct ids) | %d |" % s["rings_total"],
        "| Rings matching a known attempt | %d |" % s["rings_matching_an_attempt"],
        "| Attempts that started before the period (reported apart), caught | %s, %s |" % (
            (s.get("started_earlier") or {}).get("attempts", 0), (s.get("started_earlier") or {}).get("caught", 0)),
        "", "| Shape | Attempts | Caught | Shape matched at alert | Shape matched, latest |", "|---|---|---|---|---|",
    ]
    for shape, v in s["per_shape"].items():
        lines.append("| %s | %d | %d | %d | %d |" % (shape, v["attempts"], v["caught"], v["shape_matched"],
                                                   v["shape_matched_final"]))
    return "\n".join(lines)


def usd(x):
    return "-" if x is None else "$%s" % format(round(x), ",")


def markdown_report(summary, params=None) -> str:
    """The evaluation as a Markdown page for the repo or the pitch: the run parameters, the
    headline numbers, every table the script prints, the fastest and slowest alerts, and the
    limitations a reader must know before quoting a number.

    summary: the dict main() returns (keys "attempts", "labels", "gnn_sweep", each optional).
    params: the run parameters to list under the title (name -> value; empty ones are left out).
    """
    lines = ["# Ring-level evaluation", ""]
    params = {k: v for k, v in (params or {}).items() if v not in (None, "", False)}
    if params:
        lines += ["## Run parameters", "", "| Parameter | Value |", "|---|---|"]
        lines += ["| %s | `%s` |" % (k, v) for k, v in params.items()]
        lines.append("")
    s = summary.get("attempts")
    if s:
        earlier = s.get("started_earlier") or {}
        lines += ["## Headline numbers", "", "| Measure | Value |", "|---|---|",
                  "| Known attempts starting in the period | %d |" % s["attempts_in_period"],
                  "| Caught by a ring | %d (%s) |" % (s["caught"], pct(s["caught_share"])),
                  "| Covered (half the attempt's accounts in one ring) | %d |" % s["covered"],
                  "| Shape match rate at the first alert, among caught | %s |" % pct(s["shape_match_share"]),
                  "| Median minutes from first transfer to alert | %s |" % num(s["minutes_to_alert_median"]),
                  "| 90th percentile minutes to alert | %s |" % num(s.get("minutes_to_alert_p90")),
                  "| Median share of the money still stoppable at the alert | %s |" % pct(s["still_stoppable_median"]),
                  "| Attempts that started before the period (reported apart), caught | %s, %s |" % (
                      earlier.get("attempts", 0), earlier.get("caught", 0)),
                  "", "## Against the known attempts (HI-Small_Patterns.txt)", "",
                  "An attempt is caught when at least half of some ring's accounts belong to it; the first ring "
                  "version that does so sets the alert time. The headline denominator is the attempts whose first "
                  "transfer lies in the replayed period. Attempts that started earlier and were still running are "
                  "reported on their own line, because the replay only showed their tail.",
                  "", report(s), ""]
        if s.get("alerts_before_first_transfer"):
            lines += ["%d alert(s) came before the attempt's first transfer (the ring formed from other activity "
                      "of the same accounts); those count as 0 minutes." % s["alerts_before_first_transfer"], ""]
        ranked = sorted((c for c in s.get("caught_attempts", []) if c.get("minutes_to_alert") is not None),
                        key=lambda c: c["minutes_to_alert"])
        if ranked:
            lines += ["### Fastest and slowest alerts", "",
                      "| Attempt | Shape | Ring | Minutes to alert | Money still stoppable |", "|---|---|---|---|---|"]
            picks = ranked[:5] + ([None] if len(ranked) > 10 else []) + ranked[max(5, len(ranked) - 5):]
            for c in picks:
                lines.append("| … | | | | |" if c is None else "| %s | %s | %s | %s | %s |" % (
                    c["attempt"], c["shape"], c["ring"], num(c["minutes_to_alert"]), pct(c.get("stoppable_share"))))
            lines.append("")
    if summary.get("labels"):
        lines += ["## Against the transfer labels (clean.csv)", "", label_report(summary["labels"]), ""]
    if summary.get("gnn_sweep"):
        lines += ["## GNN sweep", "",
                  "Each row keeps only the ring versions whose gnn_score is at or above the threshold; "
                  "\"no GNN\" is the baseline.", "", sweep_report(summary["gnn_sweep"]), ""]
    if not any(summary.get(k) for k in ("attempts", "labels", "gnn_sweep")):
        lines += ["No results: give --patterns and/or --labels.", ""]
        return "\n".join(lines)
    lines += [
        "## Limitations", "",
        "- The patterns file is an incomplete answer key: it names 3,209 of the 5,177 laundering transfers, so a "
        "ring that matches no attempt is not shown to be a false alarm.",
        "- Times are data time (September 2022), not wall-clock time during the replay. Transfers in the same "
        "minute have no order inside that minute, and the alert time is the regroup that published the ring.",
        "- The alert time is the first ring version that caught the attempt, when a person could first have "
        "acted; later versions may add accounts and change the shape name (both columns are reported).",
    ]
    if params.get("patterns") and not params.get("rates"):
        lines.append("- No exchange-rate file was given: attempts paid in other currencies have no money numbers.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- command line

def main(argv=None):
    p = argparse.ArgumentParser(description="Ring-level evaluation against HI-Small_Patterns.txt and/or the labels")
    p.add_argument("--patterns", help="HI-Small_Patterns.txt (attempt-level numbers)")
    p.add_argument("--labels", help="clean.csv: ring precision and laundering coverage from the labels")
    p.add_argument("--models", help="models folder; its thresholds.json splits core from expanded transfers")
    p.add_argument("--rates", help="fx_rates.json from scripts/clean_data.py (for the money numbers)")
    p.add_argument("--splits", help="splits.json; attempts before its test_start are ignored")
    p.add_argument("--since", "--replay-start", dest="since",
                   help="count attempts starting at or after this data time, YYYY-MM-DDTHH:MM:SS "
                        "(the replayer's --start when the replay began later than test_start)")
    p.add_argument("--until", help="count attempts starting at or before this data time")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--redis", action="store_true", help="read the rings stream from Redis (REDIS_HOST)")
    src.add_argument("--rings-file", help="JSON lines, one rings message per line")
    p.add_argument("--stream", default="rings")
    p.add_argument("--out", help="write the summary as JSON here")
    p.add_argument("--markdown", "--report", dest="markdown",
                   help="also write the summary as a Markdown page here (e.g. data/evaluation/evaluation.md)")
    p.add_argument("--gnn-sweep", nargs="?", const=",".join("%g" % t for t in GNN_SWEEP),
                   help="repeat the numbers keeping only rings with gnn_score at or above each of these "
                        "comma-separated thresholds (default when given alone: %(const)s)")
    args = p.parse_args(argv)
    if not args.patterns and not args.labels:
        p.error("give --patterns and/or --labels")
    sweep = None
    if args.gnn_sweep:
        try:
            sweep = [float(x) for x in args.gnn_sweep.split(",") if x.strip()]
        except ValueError:
            p.error("--gnn-sweep takes comma-separated numbers, e.g. 0.3,0.5,0.7")

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
        raw = [f for _, f in bus.connect().xrange(args.stream, "-", "+")]
    else:
        raw = []
        with open(args.rings_file) as fh:
            raw = [json.loads(line) for line in fh if line.strip()]
    rings = [parse_ring_message(f) for f in raw]

    summary = {}
    attempts = labels = threshold = None
    if args.patterns:
        rates = load_rates(args.rates) if args.rates else {}
        attempts = load_attempts(args.patterns, rates)
        summary["attempts"] = evaluate(attempts, rings, since, until)
        print("attempts in file: %d | ring versions read: %d | period from %s" % (
            len(attempts), len(rings), since.strftime(ISO) if since else "the beginning"), file=sys.stderr)
        print(report(summary["attempts"]))
    if args.labels:
        if args.models:
            with open(Path(args.models) / "thresholds.json") as fh:
                threshold = float(json.load(fh)["ring"])
        labels = load_labels(args.labels)
        summary["labels"] = label_summary(raw, ring_threshold=threshold, labels=labels)
        print()
        print(label_report(summary["labels"]))
    if sweep is not None:
        scored = sum(1 for f in raw if gnn_score_of(f) is not None)
        print("\nGNN sweep: %d of %d ring versions carry a gnn_score" % (scored, len(raw)), file=sys.stderr)
        if not scored:
            print("no ring has a gnn_score: train the GNN (scripts/train_gnn.py) and replay again", file=sys.stderr)
        summary["gnn_sweep"] = gnn_sweep(raw, sweep, attempts, since, until, labels, threshold)
        print()
        print(sweep_report(summary["gnn_sweep"]))
    summary["meta"] = {
        "rings_source": "redis stream '%s'" % args.stream if args.redis else args.rings_file,
        "ring_versions": len(raw), "since": since.strftime(ISO) if since else None,
        "until": until.strftime(ISO) if until else None, "patterns": args.patterns, "labels": args.labels,
    }
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(summary, fh, indent=1, default=str)
    if args.markdown:
        params = {"patterns": args.patterns, "labels": args.labels, "models": args.models, "rates": args.rates,
                  "splits": args.splits, "since": since.strftime(ISO) if since else None,
                  "until": until.strftime(ISO) if until else None,
                  "rings from": summary["meta"]["rings_source"], "ring versions read": len(raw),
                  "gnn sweep": ",".join("%g" % t for t in sweep) if sweep is not None else None}
        with open(args.markdown, "w") as fh:
            fh.write(markdown_report(summary, params))
    return summary


if __name__ == "__main__":
    main()
