"""A `cases` message -> a neutral "card" (a plain dict). No chat-product code here.

The card says what to show; each adapter decides how to draw it. Every json.loads
and number conversion is guarded, so a half-broken case still gives a card, and only
a case with no ring id or no readable summary counts as malformed.

The label dicts are copied from services/page/models.py on purpose (the plan keeps
services from importing each other); keep the wording in step with the page.
"""
import json
import os
from datetime import datetime, timezone

DEFAULT_DASHBOARD_URL = "http://localhost:8501"
DASH = "—"

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

PART_LABELS = {
    "summary": "Summary",
    "what_happened": "What happened",
    "why_suspicious": "Why it is suspicious",
    "possible_victim": "Possible victim",
    "suggested_next_step": "Suggested next step",
}
PART_ORDER = tuple(PART_LABELS)

DECISION_LABELS = {
    "freeze": "Freeze recorded",
    "dismiss": "Dismissed",
    "more_info": "Needs more info",
}

BUTTONS = (
    {"decision": "freeze", "label": "Freeze accounts"},
    {"decision": "dismiss", "label": "Dismiss"},
    {"decision": "more_info", "label": "Needs more info"},
)


def dashboard_url():
    return os.environ.get("DASHBOARD_URL", "") or DEFAULT_DASHBOARD_URL


# --- small readers ----------------------------------------------------------------

def readable(key):
    text = (key or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def normalise_shape(raw):
    key = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    return key or "other"


def shape_label(raw):
    key = normalise_shape(raw)
    return SHAPE_LABELS.get(key, readable(key))


def to_float(value):
    try:
        if value is None or value == "" or value == "None":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value, default=None):
    number = to_float(value)
    return int(number) if number is not None else default


def json_value(value, default):
    """Lists and objects travel as JSON text; a parsed value passes straight through."""
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def money(value):
    number = to_float(value)
    return DASH if number is None else "${:,.0f}".format(number)


def score(value):
    number = to_float(value)
    return DASH if number is None else "%.2f" % number


def parse_time(text):
    """ISO text -> aware datetime. A naive time (the case writer's written_at) is the
    host's local time, which is UTC inside the containers."""
    text = (text or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()   # naive = local time
    return dt.astimezone(timezone.utc)


def utc_label(text):
    dt = parse_time(text)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else (text or "")


def who(decided_by):
    """'slack:U0123ABC:jane.doe' -> 'jane.doe'; 'console:aryan' -> 'aryan'."""
    text = (decided_by or "").strip()
    return text.split(":")[-1] if text else ""


# --- the case ---------------------------------------------------------------------

def parse(case):
    """A `cases` message (all text) -> a dict with real types. Safe to call twice.
    Raises ValueError only when there is no ring id or no usable summary."""
    if getattr(case, "get", None) is None:
        raise ValueError("case is not a mapping")
    if case.get("_parsed"):
        return case
    ring_id = str(case.get("ring_id") or "").strip()
    if not ring_id:
        raise ValueError("case without ring_id")
    summary = json_value(case.get("summary"), None)
    if not isinstance(summary, dict) or not any(str(summary.get(k) or "").strip() for k in PART_ORDER):
        raise ValueError("case %s has no readable summary" % ring_id)
    facts = json_value(case.get("facts"), {})
    if not isinstance(facts, dict):
        facts = {}
    return {
        "_parsed": True,
        "ring_id": ring_id,
        "version": to_int(case.get("version"), 0),
        "summary": summary,
        "source": str(case.get("source") or "template").strip().lower(),
        "check_note": str(case.get("check_note") or ""),
        "written_at": str(case.get("written_at") or ""),
        "facts": facts,
        # optional extras the case writer may add; never required
        "written_after_s": to_float(case.get("written_after_s")),
        "ring_last_seen": str(case.get("ring_last_seen") or ""),
    }


def rank(case):
    """Which case is newer for a ring: higher version, then Granite over template
    (the case writer publishes the template first and Granite seconds later)."""
    return (case["version"], 1 if case["source"] == "granite" else 0)


def granite_failed(case):
    note = case["check_note"].strip().lower()
    return case["source"] != "granite" and (note.startswith("granite error") or note.startswith("granite reply rejected"))


def source_line(case):
    if case["source"] == "granite":
        if case["check_note"].strip().lower() == "passed fact check":
            return "Written by Granite, passed the fact check"
        return "Written by Granite"
    if granite_failed(case):
        return "Template summary: Granite failed or its reply failed the fact check for this version"
    return "Template summary (Granite not used for this version)"


def decision_view(decision):
    """A decisions message -> what the card shows about it."""
    if not decision:
        return None
    kind = (decision.get("decision") or "").strip().lower()
    label = DECISION_LABELS.get(kind, readable(kind))
    by = who(decision.get("decided_by"))
    text = label
    if by:
        text += " by @" + by
    if decision.get("decided_at"):
        text += " at " + utc_label(decision["decided_at"])
    if decision.get("version"):
        text += " (v%s)" % decision["version"]
    if decision.get("channel"):
        text += " via " + decision["channel"]
    return {
        "decision": kind,
        "label": label,
        "by": by,
        "version": to_int(decision.get("version")),
        "note": decision.get("note", ""),
        "text": text,
    }


def decided_for(decision, version):
    """True when this decision already covers the given ring version, so the buttons
    go away. A decision on v1 leaves v2's buttons live: the ring changed."""
    if not decision:
        return False
    seen = to_int(decision.get("version"))
    version = to_int(version)
    if seen is None or version is None:
        return True
    return seen >= version


def _fields(facts):
    if not facts:
        return []
    out = [
        ("Accounts", str(facts["n_accounts"]) if facts.get("n_accounts") is not None else DASH),
        ("Total moved", money(facts.get("total_usd"))),
        ("Transfer model", score(facts.get("xgb_score"))),
        ("Graph model", score(facts.get("gnn_score"))),
    ]
    if facts.get("key_account"):
        out.append(("Possible victim", str(facts["key_account"])))
    if facts.get("passed_on_pct") is not None:
        text = "%s%%" % facts["passed_on_pct"]
        if facts.get("hours_to_pass_on") is not None:
            text += " within %s h" % facts["hours_to_pass_on"]
        out.append(("Passed on", text))
    if facts.get("collector"):
        out.append(("Collector", str(facts["collector"])))
    reasons = facts.get("top_reasons") or []
    if isinstance(reasons, list) and reasons:
        out.append(("Top reasons", "; ".join(str(x) for x in reasons)))
    return out


def build(case, decision=None, markers=(), notes=(), footer_notes=(), dashboard=None):
    """The neutral card for one case.

    decision      the ring's latest decisions message (or None)
    markers       short lines at the top, e.g. "updated to v3", "Granite summary arrived"
    notes         lines under the title, e.g. the related-ring note
    footer_notes  extra small lines at the bottom, e.g. the suppression footer
    """
    case = parse(case)
    facts = case["facts"]
    shape = facts.get("shape")
    title = "Ring %s" % case["ring_id"]
    if shape:
        title += " · " + shape_label(shape)
    if facts.get("n_accounts") is not None:
        title += " · %s accounts" % facts["n_accounts"]

    parts = [(k, PART_LABELS[k], str(case["summary"].get(k) or "").strip()) for k in PART_ORDER]
    parts = [p for p in parts if p[2]]

    footer = [source_line(case)]
    if case["check_note"]:
        footer.append("Check: " + case["check_note"])
    written = "Written " + utc_label(case["written_at"]) if case["written_at"] else ""
    if case["written_after_s"] is not None:
        written += " (%.1f s after the ring)" % case["written_after_s"]
    if written:
        footer.append(written.strip())
    footer.extend(footer_notes)

    seen = decision_view(decision)
    buttons = [] if decided_for(decision, case["version"]) else [dict(b) for b in BUTTONS]

    return {
        "ring_id": case["ring_id"],
        "version": case["version"],
        "title": title,
        "shape": shape_label(shape) if shape else "",
        "source": case["source"],
        "fields": _fields(facts),
        "parts": parts,
        "markers": list(markers),
        "notes": list(notes),
        "decision": seen,
        "buttons": buttons,
        "footer": footer,
        "dashboard_url": dashboard or dashboard_url(),
    }


def fallback_text(card):
    """One line for notifications and for clients that cannot draw cards."""
    first = card["parts"][0][2] if card["parts"] else ""
    text = card["title"]
    if card["markers"]:
        text += " (" + ", ".join(card["markers"]) + ")"
    if first:
        text += ": " + first
    return text[:300]
