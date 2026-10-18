"""Writes an investigator's decision to the `decisions` stream. No Streamlit.

Fields: ring_id, decision (freeze, dismiss or more_info), note, decided_at (UTC),
plus version, an extra field so the audit trail says which version of the ring
the person saw. Readers that only know the guide's four fields can ignore it.
"""
from datetime import datetime, timezone
import hashlib
import json

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


def write(r, ring_id: str, version, decision: str, note: str = "", request_id=None) -> str:
    """Append a decision. Optional idempotency protects retries after a lost ACK.

    The original stream fields stay intact. A short-lived side key and XADD are
    committed together, so a connection failure cannot create duplicate retries.
    Reusing a request with a different payload is rejected instead of reporting a
    different decision as saved.
    """
    fields = build(ring_id, version, decision, note)
    if not request_id:
        return r.xadd(bus.DECISIONS, fields)
    from redis.exceptions import WatchError
    digest = hashlib.sha256(str(request_id).encode()).hexdigest()
    key = "page:decision-request:" + digest
    payload = json.dumps({k: v for k, v in fields.items() if k != "decided_at"}, sort_keys=True)
    for _ in range(3):
        with r.pipeline() as pipe:
            try:
                pipe.watch(key)
                previous = pipe.get(key)
                if previous is not None:
                    if previous != payload:
                        raise DecisionError("This pending request has different content. Retry the original decision first.")
                    return "already-recorded"
                pipe.multi()
                pipe.set(key, payload, ex=86400)
                pipe.xadd(bus.DECISIONS, fields)
                return pipe.execute()[1]
            except WatchError:
                continue
    raise DecisionError("Another submission is in progress. Retry this decision.")
