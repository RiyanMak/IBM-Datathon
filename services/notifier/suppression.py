"""Alert suppression: a ring an investigator dismissed is not re-posted for a while.

Source of truth is the `decisions` stream (passed in as {ring_id: latest decision}),
never a flag in memory, so a restart keeps every suppression and the page sees the
same decisions.

Rules (plan section 7):
- suppressed when the ring's latest decision is `dismiss` and it was made within
  SUPPRESS_HOURS (default 24) of the new case's written_at. Both are wall-clock UTC;
- a material change lifts it: n_accounts grows by at least 2, or total_usd grows by
  at least 50%, compared with the version that was dismissed;
- a ring sharing at least half its accounts with a dismissed ring is posted with a
  "related ring" note, never suppressed;
- freeze and more_info never suppress.
"""
import os
from datetime import timedelta

from services.notifier.cards import money, parse_time, to_float, to_int, utc_label, who

DEFAULT_HOURS = 24.0
GROWTH_ACCOUNTS = 2
GROWTH_TOTAL = 1.5
RELATED_SHARE = 0.5


def suppress_hours():
    value = to_float(os.environ.get("SUPPRESS_HOURS"))
    return DEFAULT_HOURS if value is None or value < 0 else value


def within_window(decision, written_at, hours):
    """True when the decision is recent enough, seen from the case's written_at.
    A case written before the decision (an old pending case) counts as inside."""
    decided = parse_time(decision.get("decided_at"))
    written = parse_time(written_at)
    if decided is None:
        return False
    if written is None:
        return True   # no clock on the case: trust the dismissal
    return written - decided < timedelta(hours=hours)


def active_dismissal(decision, written_at, hours):
    return bool(decision) and (decision.get("decision") or "").strip().lower() == "dismiss" \
        and within_window(decision, written_at, hours)


def material_change(old_facts, new_facts):
    """'' when nothing material changed, else a short reason for the re-opened marker."""
    if not old_facts or not new_facts:
        return ""
    old_n, new_n = to_int(old_facts.get("n_accounts")), to_int(new_facts.get("n_accounts"))
    if old_n is not None and new_n is not None and new_n - old_n >= GROWTH_ACCOUNTS:
        return "ring grew from %d to %d accounts" % (old_n, new_n)
    old_t, new_t = to_float(old_facts.get("total_usd")), to_float(new_facts.get("total_usd"))
    if old_t and new_t is not None and old_t > 0 and new_t >= GROWTH_TOTAL * old_t:
        return "total grew from %s to %s" % (money(old_t), money(new_t))
    return ""


def describe(decision):
    by = who(decision.get("decided_by"))
    text = "dismissed"
    if by:
        text += " by @" + by
    return text + " at " + utc_label(decision.get("decided_at"))


def check(case, latest, dismissed_facts=None, hours=None, reopened_for=""):
    """Should this case be posted? `case` is a parsed case (cards.parse).

    dismissed_facts(ring_id, version) -> facts of the dismissed version, or None
    reopened_for   decided_at of a dismissal this ring was already re-opened after,
                   so a re-opened ring is not re-opened again on every version

    Returns {"suppress": bool, "dismissal": decision or None, "reopened": marker or ""}.
    A "reopened" marker means: post a fresh message (the ring grew, or the window ended).
    """
    hours = suppress_hours() if hours is None else hours
    decision = latest.get(case["ring_id"])
    dismissed = bool(decision) and (decision.get("decision") or "").strip().lower() == "dismiss"
    if not dismissed:
        return {"suppress": False, "dismissal": None, "reopened": ""}
    if reopened_for and reopened_for == decision.get("decided_at"):
        return {"suppress": False, "dismissal": decision, "reopened": ""}
    if not within_window(decision, case["written_at"], hours):
        # the window is over: notify again (a fresh message), once per dismissal
        return {"suppress": False, "dismissal": decision,
                "reopened": "posted again: dismissed more than %g h ago" % hours}
    old = dismissed_facts(case["ring_id"], decision.get("version")) if dismissed_facts else None
    change = material_change(old, case["facts"])
    if change:
        return {"suppress": False, "dismissal": decision, "reopened": "re-opened: " + change}
    return {"suppress": True, "dismissal": decision, "reopened": ""}


def overlap(accounts, other):
    """Share of `accounts` that also belong to `other` (0 when either is empty)."""
    mine = set(accounts or ())
    if not mine:
        return 0.0
    return len(mine & set(other or ())) / len(mine)


def related_notes(ring_id, accounts, written_at, latest, ring_accounts, hours=None):
    """Notes for a ring that shares at least half its accounts with a dismissed ring.

    ring_accounts(ring_id) -> account list of that ring, or None when unknown.
    """
    hours = suppress_hours() if hours is None else hours
    mine = set(accounts or ())
    notes = []
    for other_id, decision in sorted(latest.items()):
        if other_id == ring_id or not active_dismissal(decision, written_at, hours):
            continue
        theirs = ring_accounts(other_id)
        if theirs and overlap(mine, theirs) >= RELATED_SHARE:
            notes.append("related ring %s %s (%d of %d accounts shared)" % (
                other_id, describe(decision), len(mine & set(theirs)), len(mine)))
    return notes


def suppressed_footer(hours):
    hours_text = ("%g" % hours)
    return "Dismissed: new versions are not re-posted for %s h unless the ring grows" % hours_text
