"""Turns one `cases` message into a neutral "card": a plain dict any chat adapter can draw.

    from services.notifier import cards
    card = cards.build(case_fields, ring_fields, status, markers, dashboard_url)

No Slack, no Redis, no classes. The Slack adapter turns a card into Block Kit, the
console adapter into text, a future Teams adapter into an Adaptive Card; none of
them has to know what a facts pack is or call json.loads.

The card never crashes on bad data. A case with no facts, broken JSON or a missing
GNN score still gives a card: the missing values become None (shown as a dash by
the helpers below) and the missing lists become empty.

Label maps are copied from services/page/models.py on purpose (the plan says copy
the small dict rather than import across services). Unknown values fall back to a
readable form of the raw value, the same rule the page uses.
"""
import json
import math

from services.casewriter.template import REQUIRED_KEYS

DASH = "–"

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

NO_DECISION_LABEL = "Awaiting decision"
MAX_KEY_FACTS = 8


# --- small parsers ------------------------------------------------------------

def to_float(value):
    """'0.93' -> 0.93; '', None, 'nan', 'abc' -> None. Never 0 for a missing value."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def to_int(value):
    f = to_float(value)
    return int(f) if f is not None else None


def json_field(value, default):
    """A JSON text field (or an already parsed value), or `default` if missing or broken."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def normalise_shape(raw):
    """'CYCLE', 'Scatter gather', 'fan_out' -> 'cycle', 'scatter-gather', 'fan-out'."""
    key = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    return key or "other"


def readable(key):
    """Fallback label: 'draft_report' -> 'Draft report'."""
    text = str(key or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def shape_label(raw):
    key = normalise_shape(raw)
    return SHAPE_LABELS.get(key, readable(key))


def source_label(source):
    return SOURCE_LABELS.get(source, source or "Unknown source")


def part_label(key):
    return PART_LABELS.get(key, readable(key))


def decision_label(decision):
    return DECISION_LABELS.get(decision, readable(decision))


# --- display helpers (a dash for anything missing) -----------------------------

def money(x):
    """12345.6 -> '$12,346'; None -> '–'."""
    x = to_float(x)
    return DASH if x is None else "${:,.0f}".format(x)


def pct(x):
    """98 -> '98%'; None -> '–'."""
    x = to_float(x)
    return DASH if x is None else "{:g}%".format(round(x, 1))


def hours(x):
    """0.7 -> '0.7 h'; None -> '–'."""
    x = to_float(x)
    return DASH if x is None else "{:g} h".format(round(x, 1))


def score(x):
    """0.9312 -> '0.93'; None -> '–'."""
    x = to_float(x)
    return DASH if x is None else "{:.2f}".format(x)


def short_time(iso_text):
    """'2026-10-18T14:02:11.123+00:00' -> '2026-10-18 14:02 UTC'. Unreadable text comes back as is."""
    text = str(iso_text or "")
    if len(text) < 16 or text[10] not in "T ":
        return text
    suffix = " UTC" if text.endswith(("+00:00", "Z")) else ""
    return text[:10] + " " + text[11:16] + suffix


def who(decision):
    """The display name in a decision: 'slack:U0123ABC:jane.doe' -> 'jane.doe',
    'console:aryan' -> 'aryan'. A decision from the page carries no decided_by."""
    by = str((decision or {}).get("decided_by") or "").strip()
    if not by:
        return ""
    return by.rsplit(":", 1)[-1] or by


# --- pieces of the card ---------------------------------------------------------

def key_facts(facts):
    """Up to eight (label, value text) pairs, in reading order. Facts that are absent
    (no collector, nothing passed on, no near-threshold transfers) are left out."""
    f = facts if isinstance(facts, dict) else {}
    received, sent = to_float(f.get("key_received_usd")), to_float(f.get("key_sent_usd"))
    passed, after = to_float(f.get("passed_on_pct")), to_float(f.get("hours_to_pass_on"))
    under, n_transfers = to_int(f.get("n_just_under_threshold")), to_int(f.get("n_transfers"))
    rows = [
        ("Key account (possible victim)", f.get("key_account") or None, str(f.get("key_account"))),
        ("Received", received or None, money(received)),
        ("Sent on", sent or None, money(sent)),
        ("Share passed on", passed, pct(passed)),
        ("Began passing on after", after, hours(after)),
        ("Collector", f.get("collector") or None, str(f.get("collector"))),
        ("Origin", f.get("origin") or None, str(f.get("origin"))),
        ("Just under reporting threshold", under or None, "%s transfers" % under),
        ("Transfers", n_transfers, str(n_transfers)),
    ]
    return [(label, text) for label, value, text in rows if value is not None][:MAX_KEY_FACTS]


def summary_parts(value):
    """The five summary parts in REQUIRED_KEYS order; a missing part is ''."""
    obj = json_field(value, {})
    if not isinstance(obj, dict):
        obj = {"summary": str(obj)}
    return [(part_label(k), str(obj.get(k) or "")) for k in REQUIRED_KEYS]


def status_of(decision):
    """The status block from a decision dict (or None for an open ring).
    `label` is the decision word ("Frozen"), or "Awaiting decision" for an open ring."""
    if not decision or not decision.get("decision"):
        return {"decision": None, "decided_by": "", "decided_at": "", "note": "",
                "label": NO_DECISION_LABEL}
    kind = str(decision.get("decision")).strip().lower()
    label = decision_label(kind)   # just the word; adapters add who and when themselves
    return {"decision": kind, "decided_by": str(decision.get("decided_by") or ""),
            "decided_at": str(decision.get("decided_at") or ""),
            "note": str(decision.get("note") or ""), "label": label}


def _ring_total(ring):
    edges = json_field(ring.get("edges"), [])
    total, seen = 0.0, False
    for e in edges if isinstance(edges, list) else []:
        amount = to_float(e.get("amount_usd") if isinstance(e, dict)
                          else (e[3] if isinstance(e, (list, tuple)) and len(e) > 3 else None))
        if amount is not None:
            total, seen = total + amount, True
    return total if seen else None


def _first(*values):
    for v in values:
        if v is not None:
            return v
    return None


def build(case_fields, ring_fields=None, status=None, markers=(), dashboard_url=""):
    """One card. `status` is a decision dict (as read from `decisions`) or None.

    Every key below is part of the adapters' contract; do not rename them.
    """
    case = case_fields or {}
    ring = ring_fields or {}
    facts = json_field(case.get("facts"), {})
    if not isinstance(facts, dict):
        facts = {}

    reasons = facts.get("top_reasons")
    if not isinstance(reasons, list) or not reasons:
        reasons = json_field(ring.get("reasons"), [])
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = []

    shape = str(facts.get("shape") or ring.get("shape") or "")
    source = str(case.get("source") or "").strip().lower()
    st = status_of(status)
    return {
        "ring_id": str(case.get("ring_id") or ring.get("ring_id") or ""),
        "version": str(case.get("version") or ring.get("version") or ""),
        "shape": shape,
        "shape_label": shape_label(shape),
        "n_accounts": _first(to_int(facts.get("n_accounts")), to_int(ring.get("n_accounts"))),
        "xgb_score": _first(to_float(facts.get("xgb_score")), to_float(ring.get("xgb_score"))),
        "gnn_score": _first(to_float(facts.get("gnn_score")), to_float(ring.get("gnn_score"))),
        "total_usd": _first(to_float(facts.get("total_usd")), _ring_total(ring)),
        "facts": facts,
        "key_facts": key_facts(facts),
        "summary": summary_parts(case.get("summary")),
        "reasons": [str(x) for x in reasons][:3],
        "source": source,
        "source_label": source_label(source),
        "check_note": str(case.get("check_note") or ""),
        "written_at": str(case.get("written_at") or ""),
        "dashboard_url": dashboard_url or "",
        "status": st,
        "markers": [str(m) for m in markers or ()],
        "buttons_enabled": st["decision"] is None,
    }


CARD_KEYS = ("ring_id", "version", "shape", "shape_label", "n_accounts", "xgb_score", "gnn_score",
             "total_usd", "facts", "key_facts", "summary", "reasons", "source", "source_label",
             "check_note", "written_at", "dashboard_url", "status", "markers", "buttons_enabled")
