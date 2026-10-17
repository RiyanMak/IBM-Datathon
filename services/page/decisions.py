"""Writes an investigator's decision to the `decisions` stream. No Streamlit.

Fields: ring_id, decision (freeze, dismiss or more_info), note, decided_at (UTC),
plus version, an extra field so the audit trail says which version of the ring
the person saw. Readers that only know the guide's four fields can ignore it.
"""
from datetime import datetime, timezone

from common import bus
from services.page.models import DECISIONS

MAX_NOTE = 1000


class DecisionError(ValueError):
    pass


def build(ring_id: str, version, decision: str, note: str = "", now=None) -> dict:
    """The message to send, all values as text. Raises DecisionError on bad input."""
    ring_id = (ring_id or "").strip()
    decision = (decision or "").strip().lower()
    if not ring_id:
        raise DecisionError("no ring selected")
    if decision not in DECISIONS:
        raise DecisionError("decision must be one of %s, not %r" % (", ".join(DECISIONS), decision))
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise DecisionError("note is %d characters; the limit is %d" % (len(note), MAX_NOTE))
    now = now or datetime.now(timezone.utc)
    return {
        "ring_id": ring_id,
        "version": str(int(version)) if version is not None else "",
        "decision": decision,
        "note": note,
        "decided_at": now.isoformat(timespec="milliseconds"),
    }


def write(r, ring_id: str, version, decision: str, note: str = "") -> str:
    """Append one decision. Returns the Redis message ID."""
    return r.xadd(bus.DECISIONS, build(ring_id, version, decision, note))
