"""Writes and reads investigator decisions on the `decisions` stream, for the notifier.

The page writes decisions too (services/page/decisions.py). To keep one audit trail
the notifier writes the page's five fields with the page's own function, so the
formats can never drift:

    ring_id, version, decision (freeze | dismiss | more_info), note (<= 1000 chars),
    decided_at (UTC ISO with milliseconds, e.g. 2026-10-18T14:02:11.123+00:00)

and adds two fields the page does not write:

    decided_by   who clicked: "slack:U0123ABC:jane.doe" or "console:aryan"
    channel      where they clicked: "slack" | "teams" | "console" | "page"

Readers that know only the page's fields ignore the extras, the same rule the page
already follows for `version`.

Reading: the latest decision per ring is the newest message for that ring in stream
order (XREVRANGE, bounded), exactly what the page's state.py does, so the card and
the dashboard badge always agree.
"""
from common import bus
from services.page import decisions as page_decisions
from services.page.decisions import DecisionError  # noqa: F401  (re-exported for callers)

CHANNELS = ("slack", "teams", "console", "page")
WINDOW = 500


def build(ring_id, version, decision, note, decided_by, channel, now=None):
    """The message to send, all values text. Raises DecisionError on bad input."""
    channel = (channel or "").strip().lower()
    if channel not in CHANNELS:
        raise DecisionError("channel must be one of %s, not %r" % (", ".join(CHANNELS), channel))
    decided_by = (decided_by or "").strip()
    if not decided_by:
        raise DecisionError("decided_by is empty")
    if version == "":
        version = None
    try:
        msg = page_decisions.build(ring_id, version, decision, note, now=now)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, DecisionError):
            raise
        raise DecisionError("version must be a whole number, not %r" % (version,))
    msg["decided_by"] = decided_by[:200]
    msg["channel"] = channel
    return msg


def write(r, ring_id, version, decision, note, decided_by, channel, now=None):
    """Append one decision. Returns the Redis message ID."""
    return r.xadd(bus.DECISIONS, build(ring_id, version, decision, note, decided_by, channel, now))


def latest(r, count=WINDOW):
    """ring_id -> the newest decision dict for that ring (by stream order)."""
    out = {}
    for _msg_id, f in r.xrevrange(bus.DECISIONS, "+", "-", count=count):
        ring_id = (f or {}).get("ring_id")
        if ring_id:
            out.setdefault(ring_id, dict(f))
    return out


def history(r, ring_id, count=WINDOW):
    """Every decision for one ring, oldest first."""
    rows = r.xrevrange(bus.DECISIONS, "+", "-", count=count)
    return [dict(f) for _msg_id, f in reversed(rows) if (f or {}).get("ring_id") == ring_id]
