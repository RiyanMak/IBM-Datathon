"""Asks the case writer for a Granite summary of one ring, now. No Streamlit.

The case writer gives every ring a template case at once and, in its default
on_demand mode, only calls Granite for rings someone is looking at. The page
writes one message on the `granite_requests` stream when an investigator opens
a ring whose newest case is still the template; the case writer answers in one
Granite call's time and keeps that ring fresh for a few minutes afterwards.

Fields: ring_id, version (the one on screen), requested_at (UTC), requested_by
("page" or "flask"). A request for a ring that already has Granite's text is
ignored by the case writer, so asking again is harmless; `Throttle` keeps the
page from asking every refresh anyway.
"""
from datetime import datetime, timezone
import time

from common import bus

REPEAT_SECONDS = 45   # ask again for the same ring version only after this long
# check_note values the case writer leaves on a template case while Granite may still write
PENDING_NOTES = ("granite pending", "granite on request")


def granite_expected(case) -> bool:
    """True when the case writer may still replace this template with Granite's text."""
    return case is not None and not case.is_granite and case.check_note.strip().lower() in PENDING_NOTES


def build(ring_id: str, version, requested_by: str = "page", now=None) -> dict:
    ring_id = (ring_id or "").strip()
    if not ring_id:
        raise ValueError("no ring to request")
    now = now or datetime.now(timezone.utc)
    return {
        "ring_id": ring_id,
        "version": str(int(version)) if version is not None else "",
        "requested_at": now.isoformat(timespec="milliseconds"),
        "requested_by": (requested_by or "page").strip(),
    }


def write(r, ring_id: str, version, requested_by: str = "page") -> str:
    return r.xadd(bus.GRANITE_REQUESTS, build(ring_id, version, requested_by))


def wanted(case) -> bool:
    """True when the newest case for the ring is not Granite's own text yet."""
    return case is None or not case.is_granite


class Throttle:
    """Remembers when each ring version was last requested: one request per
    REPEAT_SECONDS per (ring, version), kept in any dict (a session state, a module)."""

    def __init__(self, store=None, repeat_s=REPEAT_SECONDS):
        self.store = store if store is not None else {}
        self.repeat_s = repeat_s

    def due(self, ring_id, version, now=None) -> bool:
        now = now if now is not None else time.time()
        last = self.store.get(ring_id)
        return last is None or last[0] != version or now - last[1] >= self.repeat_s

    def mark(self, ring_id, version, now=None):
        self.store[ring_id] = (version, now if now is not None else time.time())

    def asked_at(self, ring_id, version):
        """When this ring version was requested, or None."""
        last = self.store.get(ring_id)
        return last[1] if last and last[0] == version else None

    def request(self, r, ring_id, version, requested_by="page", now=None) -> bool:
        """Write a request when one is due; returns whether it was written."""
        if not self.due(ring_id, version, now):
            return False
        write(r, ring_id, version, requested_by)
        self.mark(ring_id, version, now)
        return True
