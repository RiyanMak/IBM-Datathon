"""Write a decision made through the notifier to the `decisions` stream, and read
decisions back.

The first five fields are exactly what services/page/decisions.py writes (same
names, same formats), so the page shows a decision made here with no change on its
side. Two extras say who decided and where:

    decided_by   "console:aryan" (a chat adapter would write e.g. "slack:U0123ABC:jane.doe")
    channel      "console" today; "slack", "teams" and "page" are reserved in the contract

Readers that know only the guide's four fields ignore the extras.
"""
from datetime import datetime, timezone

from common import bus

DECISIONS = ("freeze", "dismiss", "more_info")
CHANNELS = ("slack", "teams", "console", "page")
MAX_NOTE = 1000
WINDOW = 500   # same window as the page's state.read_decisions


class DecisionError(ValueError):
    pass


def build(ring_id, version, decision, note="", decided_by="", channel="console", now=None):
    """The message to send, all values as text. Raises DecisionError on bad input."""
    ring_id = (ring_id or "").strip()
    decision = (decision or "").strip().lower()
    if not ring_id:
        raise DecisionError("no ring given")
    if decision not in DECISIONS:
        raise DecisionError("decision must be one of %s, not %r" % (", ".join(DECISIONS), decision))
    if channel not in CHANNELS:
        raise DecisionError("channel must be one of %s, not %r" % (", ".join(CHANNELS), channel))
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise DecisionError("note is %d characters; the limit is %d" % (len(note), MAX_NOTE))
    try:
        version = str(int(version)) if version not in (None, "") else ""
    except (TypeError, ValueError):
        raise DecisionError("version must be a whole number, not %r" % (version,))
    now = now or datetime.now(timezone.utc)
    return {
        "ring_id": ring_id,
        "version": version,
        "decision": decision,
        "note": note,
        "decided_at": now.isoformat(timespec="milliseconds"),
        "decided_by": (decided_by or "").strip()[:200],
        "channel": channel,
    }


def write(r, ring_id, version, decision, note="", decided_by="", channel="console", now=None):
    """Append one decision; returns the message fields that were written."""
    fields = build(ring_id, version, decision, note, decided_by, channel, now)
    r.xadd(bus.DECISIONS, fields)
    return fields


def latest(r, count=WINDOW):
    """{ring_id: newest decision} from the last `count` decisions, like the page."""
    out = {}
    for _msg_id, fields in r.xrevrange(bus.DECISIONS, "+", "-", count=count):
        ring_id = (fields.get("ring_id") or "").strip()
        if ring_id:
            out.setdefault(ring_id, fields)
    return out


def history(r, ring_id):
    """Every decision for one ring, oldest first (for the case packet's log).
    Decisions arrive at human speed, so reading the whole stream is cheap."""
    return [f for _msg_id, f in r.xrange(bus.DECISIONS, "-", "+")
            if (f.get("ring_id") or "").strip() == ring_id]
