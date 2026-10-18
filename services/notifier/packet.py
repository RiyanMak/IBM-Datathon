"""The case packet: one Markdown document per ring, the draft an investigator would file.

    from services.notifier import packet
    text = packet.render(case_fields, ring_fields, decisions, manifest)
    name = packet.filename(case_fields)          # case_R-0042_v3.md

Built from the `cases` message (the summary and the facts pack the case writer
computed), the matching `rings` message (the accounts and the transfers, which cases
do not carry) and the `decisions` messages for the ring. Every number in the summary
and the key facts comes from the facts pack, and every transfer line carries its
`txn_id`, so anyone can trace it back to clean.csv and from there to the raw CSV line.
That is the answer to "how do you know the AI did not make this up".

Sections, in order: header, summary (the five parts), key facts, accounts with roles,
transfers, data lineage, decision log, disclaimer.

Never raises on bad data. Redis hands us text; a field that is missing, empty or broken
JSON simply leaves its part of the packet short ("not available"), so a half-written
case still gives a readable packet. No Slack, no Redis here: the Slack adapter uploads
the text, the page could offer it as a download, and `check_numbers` is used by tests.

Label maps are copied from services/page/models.py on purpose (the plan says copy the
small dict rather than import across services).
"""
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from services.casewriter import factcheck
from services.casewriter.template import REQUIRED_KEYS

MAX_TRANSFER_ROWS = 200
DASH = "-"

SHAPE_LABELS = {
    "fan-out": "Fan-out",
    "fan-in": "Fan-in",
    "scatter-gather": "Scatter-gather",
    "gather-scatter": "Gather-scatter",
    "cycle": "Cycle",
    "bipartite": "Bipartite",
    "stack": "Stack",
    "other": "Other",
}

SOURCE_LABELS = {
    "granite": "Granite, fact-checked",
    "template": "Template",
}

PART_LABELS = {
    "summary": "Summary",
    "what_happened": "What happened",
    "why_suspicious": "Why it is suspicious",
    "possible_victim": "Possible victim",
    "suggested_next_step": "Suggested next step",
}

DECISION_LABELS = {
    "freeze": "Frozen",
    "dismiss": "Dismissed",
    "more_info": "Needs more info",
}

# Section headings. check_numbers finds the checked part of the packet by these.
H_SUMMARY = "## Summary"
H_FACTS = "## Key facts"
H_ACCOUNTS = "## Accounts involved"
H_TRANSFERS = "## Transfers"
H_LINEAGE = "## Data lineage"
H_DECISIONS = "## Decision log"
H_DISCLAIMER = "## Disclaimer"

NO_TRANSFERS = "No transfer detail available for this ring (the ring message was not found)."

# Used when no cleaning_manifest.json is at hand: the hash of the reference input.
FALLBACK_INPUT_SHA256 = "b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040"


# ---------------------------------------------------------------- reading text values

def _text(value):
    """Redis may hand us bytes; turn them into normal text."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def _clean(fields):
    """A message with text keys and values, or {} for anything that is not a dict."""
    if not isinstance(fields, dict):
        return {}
    return {_text(k): _text(v) for k, v in fields.items()}


def _json(value, default):
    """Lists and objects travel as JSON text. Broken or missing JSON gives the default."""
    value = _text(value)
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return default
    else:
        parsed = value
    return parsed if isinstance(parsed, type(default)) else default


def _num(value):
    """A float, or None for missing, empty, 'None' or text that is not a number."""
    value = _text(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _cell(value):
    """Text that is safe inside a Markdown table cell."""
    if value is None or value == "":
        return DASH
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip() or DASH


def _readable(key):
    text = str(key or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def _shape_label(shape):
    key = str(shape or "").strip().lower().replace("_", "-").replace(" ", "-")
    return SHAPE_LABELS.get(key, _readable(key)) if key else "Unknown shape"


def _money(x, cents=False):
    x = _num(x)
    if x is None:
        return DASH
    return ("${:,.2f}" if cents else "${:,.0f}").format(x)


def _plain(x, suffix=""):
    """A number as written in the facts pack (no rounding beyond what it already has)."""
    x = _num(x)
    if x is None:
        return DASH
    text = ("%d" % x) if x == int(x) else ("%g" % x)
    return text + suffix


def _score(x):
    x = _num(x)
    return DASH if x is None else "%.2f" % x


# ---------------------------------------------------------------- the pieces

def filename(case_fields):
    """The attachment name: case_<ring_id>_v<version>.md."""
    c = _clean(case_fields)
    ring_id = str(c.get("ring_id") or "R-unknown").replace("/", "_").replace(" ", "_")
    version = str(c.get("version") or "1").replace("/", "_").replace(" ", "_")
    return "case_%s_v%s.md" % (ring_id, version)


def load_manifest(path=None):
    """cleaning_manifest.json from the data folder (or `path`) as a dict, or None.

    The packet takes the input file's SHA-256 from it. Missing or broken file: None,
    and the packet falls back to the reference hash.
    """
    if path is None:
        data_dir = os.environ.get("DATA_DIR", "/data/data")
        path = Path(data_dir) / "cleaning_manifest.json"
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _input_sha256(manifest):
    if isinstance(manifest, dict) and manifest.get("input_sha256"):
        return str(manifest["input_sha256"])
    try:
        from scripts.validate_clean import KEVIN_REFERENCE
        return str(KEVIN_REFERENCE["input_sha256"])
    except Exception:   # scripts/ not importable (pandas missing...): same constant, copied
        return FALLBACK_INPUT_SHA256


def _parse_edge(edge):
    """One transfer as a dict of text-ish values, or None when it is unusable."""
    if isinstance(edge, (list, tuple)) and len(edge) >= 5:
        names = ["txn_id", "from", "to", "amount_usd", "ts", "score"]
        edge = dict(zip(names, list(edge) + [None] * (6 - len(edge))))
    if not isinstance(edge, dict):
        return None
    return {
        "txn_id": "" if edge.get("txn_id") is None else str(edge.get("txn_id")),
        "from": str(edge.get("from") or edge.get("from_acct") or ""),
        "to": str(edge.get("to") or edge.get("to_acct") or ""),
        "amount_usd": _num(edge.get("amount_usd")),
        "ts": "" if edge.get("ts") is None else str(edge.get("ts")),
        "score": _num(edge.get("score")),
    }


def _txn_sort_key(e):
    try:
        return (e["ts"], 0, int(e["txn_id"]), "")
    except ValueError:
        return (e["ts"], 1, 0, e["txn_id"])


def edges_of(ring_fields):
    """The ring's transfers sorted by time, broken ones left out. [] when there are none."""
    r = _clean(ring_fields)
    edges = [_parse_edge(e) for e in _json(r.get("edges"), [])]
    return sorted((e for e in edges if e is not None), key=_txn_sort_key)


def _roles(facts):
    roles = {}
    for name, label in (("key_account", "possible victim (key account)"),
                        ("origin", "origin"), ("collector", "collector")):
        acct = facts.get(name)
        if acct:
            roles.setdefault(str(acct), label)
    return roles


def _header(c, facts, ring, now):
    ring_id = c.get("ring_id") or facts.get("ring_id") or "R-unknown"
    version = c.get("version") or ring.get("version") or DASH
    shape = facts.get("shape") or ring.get("shape")
    source = c.get("source") or ""
    generated = (now or datetime.now(timezone.utc))
    if generated.tzinfo is not None:
        generated = generated.astimezone(timezone.utc)
    return [
        "# Case packet: %s, version %s" % (ring_id, version),
        "",
        "| | |",
        "|---|---|",
        "| Ring | %s |" % _cell(ring_id),
        "| Version | %s |" % _cell(version),
        "| Shape | %s |" % _cell(_shape_label(shape)),
        "| Generated at | %s UTC |" % generated.strftime("%Y-%m-%d %H:%M:%S"),
        "| Case written at | %s |" % _cell(c.get("written_at")),
        "| Narrative source | %s |" % _cell(SOURCE_LABELS.get(source, source or "Unknown source")),
        "| Fact check | %s |" % _cell(c.get("check_note")),
        "",
    ]


def _summary(c):
    parts = _json(c.get("summary"), {})
    lines = [H_SUMMARY, ""]
    for key in REQUIRED_KEYS:
        text = parts.get(key)
        text = str(text).strip() if text not in (None, "") else "(not available)"
        lines += ["### " + PART_LABELS.get(key, _readable(key)), "", text, ""]
    return lines


def _key_facts(facts):
    f = facts
    pct = "%" if _num(f.get("passed_on_pct")) is not None else ""
    hours = " hours" if _num(f.get("hours_to_pass_on")) is not None else ""
    dur = " hours" if _num(f.get("duration_hours")) is not None else ""
    under = DASH
    if _num(f.get("n_just_under_threshold")) is not None:
        under = "%s (reporting threshold %s)" % (
            _plain(f.get("n_just_under_threshold")), _money(f.get("reporting_threshold_usd")))
    reasons = f.get("top_reasons") if isinstance(f.get("top_reasons"), list) else []
    rows = [
        ("Total moved", _money(f.get("total_usd"))),
        ("Accounts", _plain(f.get("n_accounts"))),
        ("Transfers", _plain(f.get("n_transfers"))),
        ("First seen", f.get("first_seen")),
        ("Last seen", f.get("last_seen")),
        ("Duration", _plain(f.get("duration_hours"), dur)),
        ("Possible victim (key account)", f.get("key_account")),
        ("Key account received", _money(f.get("key_received_usd"))),
        ("Key account sent on", _money(f.get("key_sent_usd"))),
        ("Share passed on", _plain(f.get("passed_on_pct"), pct)),
        ("Hours until it passed money on", _plain(f.get("hours_to_pass_on"), hours)),
        ("Origin", f.get("origin")),
        ("Collector", f.get("collector")),
        ("Transfers just under the threshold", under),
        ("Model score (XGBoost, ring mean)", _score(f.get("xgb_score"))),
        ("Graph score (GNN)", _score(f.get("gnn_score"))),
        ("Top reasons", "; ".join(str(x) for x in reasons) if reasons else None),
    ]
    lines = [H_FACTS, "", "| Fact | Value |", "|---|---|"]
    lines += ["| %s | %s |" % (name, _cell(value)) for name, value in rows]
    if not facts:
        lines += ["", "The facts pack was not available for this case."]
    return lines + [""]


def _accounts(facts, ring):
    accounts = [str(a) for a in _json(ring.get("accounts"), [])]
    roles = _roles(facts)
    lines = [H_ACCOUNTS, ""]
    if not accounts:
        accounts = list(roles)
        if accounts:
            lines += ["Only the accounts named in the facts pack (the ring message was not found).", ""]
    if not accounts:
        return lines + ["No account detail available.", ""]
    ordered = [a for a in roles if a in accounts] + sorted(a for a in accounts if a not in roles)
    lines += ["| Account | Role |", "|---|---|"]
    lines += ["| %s | %s |" % (_cell(a), roles.get(a, "other")) for a in ordered]
    return lines + [""]


def _transfers(ring_fields):
    lines = [H_TRANSFERS, ""]
    if ring_fields is None:
        return lines + [NO_TRANSFERS, ""]
    edges = edges_of(ring_fields)
    if not edges:
        return lines + ["No transfer detail available for this ring.", ""]
    lines += ["| Time | txn_id | From | To | Amount (USD) | Score |", "|---|---|---|---|---|---|"]
    for e in edges[:MAX_TRANSFER_ROWS]:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            _cell(e["ts"]), _cell(e["txn_id"]), _cell(e["from"]), _cell(e["to"]),
            _money(e["amount_usd"], cents=True), _score(e["score"])))
    if len(edges) > MAX_TRANSFER_ROWS:
        lines += ["", "Showing the first %d of %d transfers, in time order. The full list is in "
                      "the ring message on the `rings` stream." % (MAX_TRANSFER_ROWS, len(edges))]
    return lines + [""]


def _lineage(manifest):
    return [H_LINEAGE, "",
            "Transaction IDs refer to clean.csv (input HI-Small_Trans.csv SHA-256 %s). Amounts in USD "
            "use exchange rates fitted on the training period only (fx_rates.json)." % _input_sha256(manifest),
            ""]


def _decisions(decisions, ring_id):
    rows = []
    for d in decisions or []:
        d = _clean(d)
        if not d:
            continue
        if ring_id and d.get("ring_id") and d.get("ring_id") != ring_id:
            continue
        rows.append(d)
    rows.sort(key=lambda d: str(d.get("decided_at") or ""))
    lines = [H_DECISIONS, ""]
    if not rows:
        return lines + ["No decision recorded yet.", ""]
    lines += ["| Decided at (UTC) | Version | Decision | By | Channel | Note |", "|---|---|---|---|---|---|"]
    for d in rows:
        decision = str(d.get("decision") or "")
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            _cell(d.get("decided_at")), _cell(d.get("version")),
            _cell(DECISION_LABELS.get(decision, _readable(decision))),
            _cell(d.get("decided_by") or "page"), _cell(d.get("channel") or "page"), _cell(d.get("note"))))
    return lines + [""]


DISCLAIMER = [
    H_DISCLAIMER, "",
    "Built from the IBM synthetic anti-money-laundering dataset (HI-Small); no real customers. "
    "This packet describes activity consistent with a money-laundering pattern; it is not an "
    "accusation. The model recommends and explains; a person decides.",
    "",
]


def render(case_fields, ring_fields=None, decisions=(), manifest=None, now=None):
    """The whole packet as Markdown text. Never raises on bad or missing fields."""
    c = _clean(case_fields)
    ring = _clean(ring_fields)
    facts = _json(c.get("facts"), {})
    ring_id = str(c.get("ring_id") or facts.get("ring_id") or ring.get("ring_id") or "")
    lines = (_header(c, facts, ring, now) + _summary(c) + _key_facts(facts) + _accounts(facts, ring)
             + _transfers(ring_fields if ring else None) + _lineage(manifest)
             + _decisions(decisions, ring_id) + DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------- the number check

def numbers_in(text):
    """Every number in a piece of text, ignoring account IDs, dates, times and ring IDs.

    The case writer's fact-check rule, reused so the packet is held to the same standard
    as a Granite reply.
    """
    return factcheck.numbers_in(str(text or ""))


def _section(text, start, end):
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i)
    return text[i:j if j >= 0 else len(text)]


def check_numbers(packet_text, facts, ring_fields=None):
    """Numbers in the summary and key facts that are in neither the facts pack nor the edges.

    Returns them as text ([] means every number traces back). The header, transfer table,
    lineage and decision log are not checked: they hold txn_ids, timestamps, versions and
    hashes, which are references rather than claims.
    """
    if isinstance(facts, str):
        facts = _json(facts, {})
    facts = facts if isinstance(facts, dict) else {}
    try:
        allowed = factcheck.allowed_numbers(facts)
    except Exception:   # a broken date in the facts: fall back to the plain numbers
        allowed = set(factcheck.ALWAYS_ALLOWED)
        allowed.update(float(v) for v in facts.values()
                       if isinstance(v, (int, float)) and not isinstance(v, bool))
    for e in edges_of(ring_fields) if ring_fields else []:
        for name in ("amount_usd", "score"):
            if e[name] is not None:
                allowed.add(float(e[name]))
    checked = _section(packet_text, H_SUMMARY, H_ACCOUNTS)
    bad = []
    for n in numbers_in(checked):
        if not any(factcheck.is_close(n, v) for v in allowed):
            bad.append("%g" % n)
    return bad
