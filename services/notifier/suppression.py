"""Alert suppression: decides whether a new case is posted, updates a card, or stays quiet.

Alert fatigue is the first complaint about real anti-money-laundering systems. When
an investigator dismisses a ring, the notifier does not post that ring again (new
versions included) for SUPPRESS_HOURS, default 24, unless the ring changed a lot.

The rules, in plain words:
- Only `dismiss` suppresses. `freeze` and `more_info` never do.
- A ring is suppressed when its latest decision is `dismiss` and that decision is
  within SUPPRESS_HOURS of the new case's `written_at`.
- A material change lifts the suppression: the ring grew by at least 2 accounts, or
  its total grew by at least 50%, compared with the card that was dismissed. The
  card is then re-posted with "re-opened: ring grew from 5 to 8 accounts".
- A new ring that shares at least half its accounts with a recently dismissed ring
  is posted as normal, with a note "related ring R-0042 dismissed by jane at ...".
  A different ring deserves a look; the note just saves the investigator a search.

The source of truth is the `decisions` stream, read by the caller and handed in
here, never an in-memory flag, so a restart keeps every suppression.

Clocks: both times are wall-clock. `decided_at` is UTC with a zone. `written_at` is
the case writer's local time with no zone; it is read as UTC. In Docker (and on the
VM) local time is UTC, so this is exact there; on a laptop in another zone the window
is shifted by the zone offset. When the case writer sends data time (`ring_last_seen`,
its plan B6) this module should switch to it.

Everything here is pure: no Redis. The notifier gathers the inputs and acts on the
answer.
"""
from datetime import datetime, timezone

from services.notifier.cards import json_field, short_time, to_float, to_int, who

ACTIONS = ("post", "update", "suppress", "skip")
GROWTH_ACCOUNTS = 2
GROWTH_TOTAL = 0.5
RELATED_SHARE = 0.5


def parse_utc(text):
    """ISO text -> an aware UTC datetime. Text with no zone is read as UTC. None if unreadable."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_suppressed(latest_decision, case_written_at, suppress_hours):
    """True when the latest decision is a dismissal made within `suppress_hours` of
    the case's `written_at`. A case written before the dismissal counts as within the
    window (it is older news). An unreadable `decided_at` never suppresses: when in
    doubt, show the card."""
    d = latest_decision or {}
    if str(d.get("decision") or "").strip().lower() != "dismiss":
        return False
    decided = parse_utc(d.get("decided_at"))
    if decided is None:
        return False
    written = parse_utc(case_written_at) or datetime.now(timezone.utc)
    hours = to_float(suppress_hours)
    hours = 24.0 if hours is None else hours
    return (written - decided).total_seconds() / 3600.0 <= hours


def material_change(dismissed_snapshot, new_facts):
    """A 're-opened: ...' marker when the ring changed enough since the dismissed card,
    else None. Unknown old or new numbers never count as a change."""
    old = dismissed_snapshot or {}
    new = new_facts if isinstance(new_facts, dict) else {}
    old_n, new_n = to_int(old.get("n_accounts")), to_int(new.get("n_accounts"))
    if old_n is not None and new_n is not None and new_n - old_n >= GROWTH_ACCOUNTS:
        return "re-opened: ring grew from %d to %d accounts" % (old_n, new_n)
    old_t, new_t = to_float(old.get("total_usd")), to_float(new.get("total_usd"))
    if old_t and new_t is not None and old_t > 0 and new_t >= old_t * (1 + GROWTH_TOTAL):
        return "re-opened: total grew from ${:,.0f} to ${:,.0f}".format(old_t, new_t)
    return None


def related_dismissed(new_accounts, dismissed_rings, ring_id=""):
    """A note when at least half of the new ring's accounts are in one dismissed ring.

    dismissed_rings: ring_id -> (accounts list, decision dict). The ring itself
    (`ring_id`) is never its own relative. The ring sharing the most accounts wins.
    """
    mine = {str(a) for a in new_accounts or ()}
    if not mine:
        return None
    best = None
    for other_id, (accounts, decision) in sorted((dismissed_rings or {}).items()):
        if other_id == ring_id:
            continue
        shared = len(mine & {str(a) for a in accounts or ()})
        if shared >= RELATED_SHARE * len(mine) and (best is None or shared > best[0]):
            best = (shared, other_id, decision)
    if best is None:
        return None
    _, other_id, decision = best
    name = who(decision)
    text = "related ring %s dismissed" % other_id
    text += (" by %s" % name) if name else " on the dashboard"
    at = short_time((decision or {}).get("decided_at"))
    return text + (" at %s" % at if at else "")


def active_decision(latest_decision, snapshot):
    """The decision the card currently reflects. A card re-posted after a dismissal
    (window over, or a material change) has set that dismissal aside; the snapshot
    remembers which one in `reopened_after`."""
    d = latest_decision
    if not d:
        return None
    if (snapshot or {}).get("reopened_after") and snapshot.get("reopened_after") == d.get("decided_at"):
        return None
    return d


def suppressed_marker(version, decision):
    name = who(decision)
    text = "suppressed v%s: dismissed" % version
    text += (" by %s" % name) if name else " on the dashboard"
    at = short_time((decision or {}).get("decided_at"))
    return text + (" at %s" % at if at else "")


def _rank(version, source):
    return (to_int(version) or 0, 1 if source == "granite" else 0)


def decide(case_fields, ring_fields, latest_decisions, snapshots, ring_accounts_index, suppress_hours):
    """(action, markers) for one case.

    action is "post" (new message), "update" (edit the ring's message in place),
    "suppress" (acknowledge and log, no new message) or "skip" (an older or identical
    case for a card that already shows something newer, e.g. a slow Granite reply for
    v1 arriving after the template for v2).

    case_fields: the `cases` message. ring_fields: the latest `rings` message for this
    ring or None. latest_decisions: ring_id -> latest decision dict. snapshots:
    ring_id -> what the notifier last posted (the `notifier:messages` hash).
    ring_accounts_index: ring_id -> accounts list, for the related-ring note.
    """
    case = case_fields or {}
    ring_id = str(case.get("ring_id") or "")
    version = str(case.get("version") or "")
    source = str(case.get("source") or "").strip().lower()
    written_at = case.get("written_at")
    facts = json_field(case.get("facts"), {})
    snap = (snapshots or {}).get(ring_id)
    decisions_ = latest_decisions or {}
    decision = active_decision(decisions_.get(ring_id), snap)

    markers = []
    dismissed = {}
    for other_id, d in decisions_.items():
        if other_id != ring_id and is_suppressed(d, written_at, suppress_hours):
            accounts = (ring_accounts_index or {}).get(other_id)
            if accounts:
                dismissed[other_id] = (accounts, d)
    accounts = json_field((ring_fields or {}).get("accounts"), [])
    related = related_dismissed(accounts if isinstance(accounts, list) else [], dismissed, ring_id)
    if related:
        markers.append(related)

    if decision and str(decision.get("decision")).lower() == "dismiss":
        if is_suppressed(decision, written_at, suppress_hours):
            change = material_change(snap, facts)
            if change:
                return "post", [change] + markers
            return "suppress", [suppressed_marker(version, decision)]
        return "post", ["re-opened: dismissed more than %g h earlier" % (to_float(suppress_hours) or 24.0)] + markers

    if not snap or not snap.get("ref"):
        return "post", markers
    new_rank, old_rank = _rank(version, source), _rank(snap.get("version"), snap.get("source"))
    if new_rank <= old_rank:
        return "skip", markers
    head = []
    if new_rank[0] > old_rank[0]:
        head.append("updated to v%s" % version)
    if source == "granite" and snap.get("source") != "granite":
        head.append("Granite summary arrived")
    return "update", head + markers
