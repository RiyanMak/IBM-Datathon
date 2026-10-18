"""The case packet: one Markdown page per ring, the draft of what an investigator files.

Every number comes from the case writer's facts pack, except the transfers table,
whose rows are the ring's own edges, each with its txn_id so anyone can trace it to
clean.csv and from there (source_row) to the raw CSV line.

    from services.notifier import packet
    text = packet.render(case, decisions, ring)

`case` is a `cases` message, `decisions` the ring's decisions oldest first, `ring`
its `rings` message (optional: without it the packet has no transfer detail).
"""
import json
import re
from datetime import datetime, timezone

from common.paths import DATA_DIR
from services.notifier.cards import (DASH, DECISION_LABELS, PART_LABELS, PART_ORDER, json_value, money,
                                     parse, parse_time, score, shape_label, source_line, to_float)

MAX_ROWS = 200
MANIFEST = "cleaning_manifest.json"
EDGE_FIELDS = ["txn_id", "from", "to", "amount_usd", "ts", "score"]   # list form, as agreed
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

DISCLAIMER = (
    "This packet was built from synthetic data (IBM AML HI-Small). It describes activity "
    "*consistent with* a money-laundering pattern; it is not an accusation. The account marked "
    "\"possible victim\" may be a recruited money mule and should be treated as a victim until "
    "reviewed. The models recommend and explain; a person decides."
)


def cell(value):
    """Text that is safe inside a Markdown table cell."""
    text = DASH if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def parse_edge(edge):
    """One edge, sent as a dict or as a list. Returns None for something unreadable."""
    if isinstance(edge, dict):
        e = {k: edge.get(k) for k in EDGE_FIELDS}
        e["from"] = e["from"] if e["from"] is not None else edge.get("from_acct")
        e["to"] = e["to"] if e["to"] is not None else edge.get("to_acct")
    elif isinstance(edge, (list, tuple)) and len(edge) >= 5:
        e = dict(zip(EDGE_FIELDS, list(edge) + [None] * (len(EDGE_FIELDS) - len(edge))))
    else:
        return None
    return {
        "txn_id": str(e["txn_id"]),
        "from": str(e["from"]),
        "to": str(e["to"]),
        "amount_usd": to_float(e["amount_usd"]),
        "ts": str(e["ts"] or ""),
        "score": to_float(e["score"]),
    }


def ring_parts(ring):
    """A `rings` message -> (accounts, edges sorted by time). Tolerates None."""
    if not ring:
        return [], []
    edges = [e for e in (parse_edge(x) for x in json_value(ring.get("edges"), []) or []) if e]
    edges.sort(key=lambda e: e["ts"])
    accounts = [str(a) for a in json_value(ring.get("accounts"), []) or []]
    if not accounts:
        accounts = sorted({e["from"] for e in edges} | {e["to"] for e in edges})
    return accounts, edges


# --- data lineage -----------------------------------------------------------------

def _hashes(node, path=""):
    """Every (path, sha256) pair anywhere in the manifest, whatever its layout."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _hashes(v, path + "/" + str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _hashes(v, path + "/" + str(i))
    elif isinstance(node, str) and SHA_RE.match(node.strip().lower()):
        yield path.lower(), node.strip().lower()


def manifest_hash(data_dir=None):
    """SHA-256 of the raw transactions file from cleaning_manifest.json, or ''."""
    data_dir = DATA_DIR if data_dir is None else data_dir
    from pathlib import Path
    for path in (Path(data_dir) / MANIFEST, Path(data_dir) / "v1" / MANIFEST):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        found = list(_hashes(manifest))
        # the transactions file is the one clean.csv comes from; else the first hash
        for p, h in found:
            if "trans" in p:
                return h
        if found:
            return found[0][1]
    return ""


def lineage(data_dir=None):
    digest = manifest_hash(data_dir)
    if digest:
        first = ("Transaction IDs refer to `clean.csv` from the cleaning run whose raw transactions "
                 "file has SHA-256 `%s` (from `cleaning_manifest.json`)." % digest)
    else:
        first = ("Transaction IDs refer to `clean.csv` from the team's cleaning run "
                 "(no `cleaning_manifest.json` was found, so the input hash is not shown).")
    return (first + " Each row of `clean.csv` keeps `source_row`, the line of the raw CSV it came from. "
            "Amounts in USD use exchange rates fitted on the training period only (`fx_rates.json`).")


# --- the packet ---------------------------------------------------------------------

def _key_facts(f):
    hours = lambda v: DASH if v is None else "%s h" % v
    pct = lambda v: DASH if v is None else "%s%%" % v
    under = DASH
    if f.get("n_just_under_threshold") is not None:
        under = str(f["n_just_under_threshold"])
        if f.get("reporting_threshold_usd") is not None:
            under += " (just under the %s reporting threshold)" % money(f["reporting_threshold_usd"])
    reasons = f.get("top_reasons") or []
    return [
        ("Total moved", money(f.get("total_usd")) if f.get("total_usd") is not None else DASH),
        ("Accounts", f.get("n_accounts")),
        ("Transfers", f.get("n_transfers")),
        ("First seen (data time)", (f.get("first_seen") or "").replace("T", " ")),
        ("Last seen (data time)", (f.get("last_seen") or "").replace("T", " ")),
        ("Duration", hours(f.get("duration_hours"))),
        ("Possible victim (key account)", f.get("key_account")),
        ("Key account received", money(f.get("key_received_usd")) if f.get("key_received_usd") is not None else DASH),
        ("Key account sent on", money(f.get("key_sent_usd")) if f.get("key_sent_usd") is not None else DASH),
        ("Share passed on", pct(f.get("passed_on_pct"))),
        ("Time to pass on", hours(f.get("hours_to_pass_on"))),
        ("Origin", f.get("origin")),
        ("Collector", f.get("collector")),
        ("Transfers just under threshold", under),
        ("Transfer model score", score(f.get("xgb_score"))),
        ("Graph model score", score(f.get("gnn_score"))),
        ("Top reasons", "; ".join(str(x) for x in reasons) if isinstance(reasons, list) else ""),
    ]


def _seconds(text):
    """Decision time to the second, in UTC (two clicks can share a minute)."""
    dt = parse_time(text)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else text


def _role(account, f):
    if account == f.get("key_account"):
        return "possible victim"
    if account == f.get("origin"):
        return "origin"
    if account == f.get("collector"):
        return "collector"
    return "other"


def render(case, decisions=(), ring=None, now=None, max_rows=MAX_ROWS, data_dir=None):
    """The packet as Markdown text."""
    case = parse(case)
    f = case["facts"]
    now = now or datetime.now(timezone.utc)
    accounts, edges = ring_parts(ring)
    lines = []
    add = lines.append

    add("# Case packet: ring %s, version %s" % (case["ring_id"], case["version"]))
    add("")
    add("- **Shape:** %s" % (shape_label(f["shape"]) if f.get("shape") else DASH))
    add("- **Prepared:** %s" % now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    add("- **Narrative:** %s" % source_line(case))
    add("- **Check note:** %s" % (case["check_note"] or DASH))
    add("")

    add("## Summary")
    add("")
    for key in PART_ORDER:
        text = str(case["summary"].get(key) or "").strip()
        add("**%s.** %s" % (PART_LABELS[key], text or DASH))
        add("")

    add("## Key facts")
    add("")
    if f:
        add("| Fact | Value |")
        add("|---|---|")
        for label, value in _key_facts(f):
            add("| %s | %s |" % (label, cell(value)))
    else:
        add("No facts pack came with this case.")
    add("")

    add("## Accounts involved")
    add("")
    if not accounts:
        accounts = [a for a in (f.get("key_account"), f.get("origin"), f.get("collector")) if a]
        if accounts:
            add("The ring message was not found, so only the accounts named in the facts are listed.")
            add("")
    if accounts:
        add("| Account | Role |")
        add("|---|---|")
        for account in accounts:
            add("| %s | %s |" % (cell(account), _role(account, f)))
    else:
        add("No account detail.")
    add("")

    add("## Transfers")
    add("")
    if not edges:
        add("No transfer detail: the ring's edges were not available when this packet was written.")
    else:
        shown = edges[:max_rows]
        if len(edges) > max_rows:
            add("Showing the first %d of %d transfers, sorted by time. The full list is in the ring "
                "message on the `rings` stream." % (max_rows, len(edges)))
            add("")
        add("| Time (data) | txn_id | From | To | Amount (USD) | Score |")
        add("|---|---|---|---|---|---|")
        for e in shown:
            amount = DASH if e["amount_usd"] is None else "${:,.2f}".format(e["amount_usd"])
            add("| %s | %s | %s | %s | %s | %s |" % (
                cell(e["ts"].replace("T", " ")), cell(e["txn_id"]), cell(e["from"]), cell(e["to"]),
                amount, score(e["score"])))
    add("")

    add("## Data lineage")
    add("")
    add(lineage(data_dir))
    add("")

    add("## Decision log")
    add("")
    if decisions:
        add("| Decided at | Decision | By | Channel | Version | Note |")
        add("|---|---|---|---|---|---|")
        for d in sorted(decisions, key=lambda d: d.get("decided_at", "")):
            kind = (d.get("decision") or "").strip().lower()
            add("| %s | %s | %s | %s | %s | %s |" % (
                cell(_seconds(d.get("decided_at"))), cell(DECISION_LABELS.get(kind, kind)),
                cell(d.get("decided_by")), cell(d.get("channel") or ("page" if not d.get("decided_by") else "")),
                cell(d.get("version")), cell(d.get("note"))))
    else:
        add("No decision recorded yet.")
    add("")

    add("## Disclaimer")
    add("")
    add(DISCLAIMER)
    add("")
    return "\n".join(lines)


def filename(case):
    case = parse(case)
    return "case_%s_v%s.md" % (case["ring_id"], case["version"])
