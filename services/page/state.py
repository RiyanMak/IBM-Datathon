"""Reads the streams and builds one PageState snapshot. No Streamlit.

    from common import bus
    from services.page import state
    snap = state.load(bus.connect())

The page only reads: no consumer group, no XACK. Every read is a bounded XREVRANGE
(never a full XRANGE of `scored`, which holds hundreds of thousands of rows).

The snapshot is built by a list of readers, one per source. A new source (say a
future LLM output on its own stream) is one new reader added to READERS plus one
new panel; nothing else changes. A reader that fails is recorded in
snapshot.errors and the others still run, so the page never crashes on bad data.
"""
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from common import bus
from services.page.models import (
    CaseView, Counters, DecisionView, Edge, PageState, RingView, SummaryPart,
    normalise_shape, part_label,
)

log = logging.getLogger(__name__)

RINGS_WINDOW = 200
CASES_WINDOW = 500
SCORED_WINDOW = 500
DECISIONS_WINDOW = 500

SCORER_STATS = "stats:scorer"          # optional: scored, flagged (asked of the scorer)
CASEWRITER_STATS = "stats:casewriter"  # optional: cases_written, granite_tried, granite_passed

EDGE_ORDER = ("txn_id", "from", "to", "amount_usd", "ts", "score")

_logged_bad = set()   # (stream, message id) already logged, so a bad message logs once


# --- small parsers ----------------------------------------------------------

def to_float(value) -> Optional[float]:
    """'0.93' -> 0.93; '', None, 'nan', 'abc' -> None."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def to_int(value, default: int = 0) -> int:
    f = to_float(value)
    return int(f) if f is not None else default


def json_field(value, default):
    """A JSON text field, or `default` if it is missing or broken."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def stream_id_ms(msg_id: str) -> int:
    """Redis stream ID '1697040000123-0' -> 1697040000123 (wall-clock ms)."""
    try:
        return int(str(msg_id).split("-", 1)[0])
    except ValueError:
        return 0


def wall_seconds(value) -> Optional[float]:
    """Wall-clock time as epoch seconds. Accepts ISO text (with or without a zone,
    'Z' allowed) or epoch seconds or milliseconds. A time without a zone is read as
    UTC; latency only ever subtracts two times from the same writer, so that is safe.
    """
    if value is None or value == "":
        return None
    num = to_float(value)
    if num is not None:
        return num / 1000.0 if num > 1e11 else num
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _bad(snap: PageState, stream: str, msg_id: str, exc: Exception):
    snap.skipped[stream] = snap.skipped.get(stream, 0) + 1
    if len(_logged_bad) > 10_000:   # the page runs for days; keep the memory bounded
        _logged_bad.clear()
    if (stream, msg_id) not in _logged_bad:
        _logged_bad.add((stream, msg_id))
        log.warning("skipped bad %s message %s: %s", stream, msg_id, exc)


# --- message -> view ----------------------------------------------------------

def parse_edge(raw) -> Edge:
    if isinstance(raw, dict):
        d = raw
    elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
        d = dict(zip(EDGE_ORDER, raw))
    else:
        raise ValueError("unrecognised edge %r" % (raw,))
    if not d.get("from") or not d.get("to"):
        raise ValueError("edge without from/to: %r" % (raw,))
    return Edge(
        txn_id=str(d.get("txn_id", "")),
        src=str(d["from"]),
        dst=str(d["to"]),
        amount_usd=to_float(d.get("amount_usd")),
        ts=str(d.get("ts", "")),
        score=to_float(d.get("score")),
    )


def parse_ring(msg_id: str, f: Dict[str, str]) -> RingView:
    ring_id = f.get("ring_id")
    if not ring_id:
        raise ValueError("ring without ring_id")
    accounts = [str(a) for a in json_field(f.get("accounts"), [])]
    edges = [parse_edge(e) for e in json_field(f.get("edges"), [])]
    reasons = json_field(f.get("reasons"), [])
    if isinstance(reasons, str):
        reasons = [reasons]
    return RingView(
        ring_id=ring_id,
        version=to_int(f.get("version"), 1),
        shape=normalise_shape(f.get("shape", "")),
        n_accounts=to_int(f.get("n_accounts"), len(accounts)),
        accounts=accounts,
        edges=edges,
        xgb_score=to_float(f.get("xgb_score")),
        gnn_score=to_float(f.get("gnn_score")),
        reasons=[str(x) for x in reasons][:3],
        first_seen=f.get("first_seen", ""),
        last_seen=f.get("last_seen", ""),
        received_ms=stream_id_ms(msg_id),
    )


def parse_summary(value) -> List[SummaryPart]:
    """Summary parts in the order they arrive; unknown keys get a readable label."""
    obj = json_field(value, None)
    if obj is None:
        obj = {"summary": value} if value else {}
    elif not isinstance(obj, dict):
        obj = {"summary": str(obj)}
    return [SummaryPart(key=k, label=part_label(k), text=str(v))
            for k, v in obj.items() if v not in (None, "")]


def parse_case(msg_id: str, f: Dict[str, str]) -> CaseView:
    ring_id = f.get("ring_id")
    if not ring_id:
        raise ValueError("case without ring_id")
    facts = json_field(f.get("facts"), None)
    return CaseView(
        ring_id=ring_id,
        version=to_int(f.get("version"), 1),
        parts=parse_summary(f.get("summary")),
        source=(f.get("source") or "").strip().lower(),
        check_note=f.get("check_note", ""),
        written_at=f.get("written_at", ""),
        facts=facts if isinstance(facts, dict) else None,
        received_ms=stream_id_ms(msg_id),
    )


def parse_decision(f: Dict[str, str]) -> DecisionView:
    ring_id = f.get("ring_id")
    if not ring_id:
        raise ValueError("decision without ring_id")
    version = to_float(f.get("version"))
    return DecisionView(
        ring_id=ring_id,
        version=int(version) if version is not None else None,
        decision=(f.get("decision") or "").strip().lower(),
        note=f.get("note", ""),
        decided_at=f.get("decided_at", ""),
    )


def case_rank(c: CaseView):
    """Which case wins for a ring: highest version; at equal version a Granite case
    beats a template one (template is written first, Granite seconds later); then
    the newest message."""
    return (c.version, 1 if c.is_granite else 0, c.received_ms)


# --- readers ------------------------------------------------------------------

def read_rings(r, snap: PageState):
    latest: Dict[str, RingView] = {}
    for msg_id, f in r.xrevrange(bus.RINGS, "+", "-", count=RINGS_WINDOW):
        try:
            ring = parse_ring(msg_id, f)
        except Exception as exc:
            _bad(snap, bus.RINGS, msg_id, exc)
            continue
        old = latest.get(ring.ring_id)
        if old is None or (ring.version, ring.received_ms) > (old.version, old.received_ms):
            latest[ring.ring_id] = ring
    snap.rings = sort_rings(list(latest.values()), "newest")


def read_cases(r, snap: PageState):
    latest: Dict[str, CaseView] = {}
    tried = passed = 0
    for msg_id, f in r.xrevrange(bus.CASES, "+", "-", count=CASES_WINDOW):
        try:
            case = parse_case(msg_id, f)
        except Exception as exc:
            _bad(snap, bus.CASES, msg_id, exc)
            continue
        if case.is_granite:
            tried += 1
            passed += 1
        elif case.check_note.startswith(("granite reply rejected", "granite error")):
            tried += 1
        old = latest.get(case.ring_id)
        if old is None or case_rank(case) > case_rank(old):
            latest[case.ring_id] = case
    snap.cases = latest
    snap.counters.granite_tried = tried
    snap.counters.granite_passed = passed


def read_casewriter_stats(r, snap: PageState):
    """Prefer the case writer's own counters when it keeps them (its plan, C2)."""
    stats = r.hgetall(CASEWRITER_STATS)
    if stats and "granite_tried" in stats:
        snap.counters.granite_tried = to_int(stats.get("granite_tried"))
        snap.counters.granite_passed = to_int(stats.get("granite_passed"))
        snap.counters.granite_from_stats = True


def read_scored(r, snap: PageState):
    c = snap.counters
    c.scored_total = r.xlen(bus.SCORED)
    rows = r.xrevrange(bus.SCORED, "+", "-", count=SCORED_WINDOW)
    flagged, latencies = 0, []
    for msg_id, f in rows:
        if f.get("flagged") == "1":
            flagged += 1
        sent, scored = wall_seconds(f.get("sent_at")), wall_seconds(f.get("scored_at"))
        if sent is not None and scored is not None and scored >= sent:
            latencies.append((scored - sent) * 1000.0)
    c.flagged, c.flagged_window = flagged, len(rows)
    c.p50_ms, c.p99_ms = percentile(latencies, 50), percentile(latencies, 99)
    c.latency_samples = len(latencies)

    stats = r.hgetall(SCORER_STATS)
    if stats and "scored" in stats:
        c.scored_total = to_int(stats.get("scored"))
        c.flagged = to_int(stats.get("flagged"))
        c.flagged_window = c.scored_total
        c.counts_exact = True


def read_decisions(r, snap: PageState):
    latest: Dict[str, DecisionView] = {}
    # Newest first, so the first decision seen for a ring is its latest.
    for msg_id, f in r.xrevrange(bus.DECISIONS, "+", "-", count=DECISIONS_WINDOW):
        try:
            d = parse_decision(f)
        except Exception as exc:
            _bad(snap, bus.DECISIONS, msg_id, exc)
            continue
        latest.setdefault(d.ring_id, d)
    snap.decisions = latest


Reader = Callable[[object, PageState], None]

READERS: List[Reader] = [read_rings, read_cases, read_casewriter_stats, read_scored, read_decisions]


def _finish(snap: PageState):
    c = snap.counters
    c.total_rings = len(snap.rings)
    c.open_rings = sum(1 for ring in snap.rings if ring.ring_id not in snap.decisions)


def load(r, readers: Optional[List[Reader]] = None) -> PageState:
    """One snapshot of everything the page shows. Never raises."""
    import redis   # only here, so models and tests of pure helpers need no redis

    snap = PageState(loaded_ms=int(time.time() * 1000))
    for reader in readers or READERS:
        try:
            reader(r, snap)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            snap.redis_ok = False
            snap.errors.append("Redis not reachable at %s:%d (%s)" % (
                bus.redis_host(), bus.redis_port(), exc.__class__.__name__))
            break   # every other reader would fail the same way
        except Exception as exc:
            log.exception("reader %s failed", getattr(reader, "__name__", reader))
            snap.errors.append("%s failed: %s" % (getattr(reader, "__name__", "reader"), exc))
    _finish(snap)
    return snap


# --- helpers the view uses ----------------------------------------------------

def sort_rings(rings: List[RingView], by: str = "newest") -> List[RingView]:
    """'newest': latest data time first. 'score': highest XGBoost score first."""
    if by == "score":
        return sorted(rings, key=lambda x: (x.xgb_score is not None, x.xgb_score or 0.0,
                                            x.received_ms), reverse=True)
    return sorted(rings, key=lambda x: (x.last_seen, x.received_ms), reverse=True)


def filter_rings(rings: List[RingView], shapes=None, min_score: float = 0.0,
                 open_only: bool = False, decided=()) -> List[RingView]:
    """Keep rings whose shape is in `shapes` (all when empty), whose XGBoost score is at
    least `min_score` (a ring with no score only passes at 0), and, with open_only, that
    have no decision. Order is kept."""
    shapes = set(shapes or ())
    out = []
    for ring in rings:
        if shapes and ring.shape not in shapes:
            continue
        if min_score > 0 and (ring.xgb_score is None or ring.xgb_score < min_score):
            continue
        if open_only and ring.ring_id in decided:
            continue
        out.append(ring)
    return out


def detect_upgrades(seen: Dict[str, tuple], cases: Dict[str, CaseView]) -> List[str]:
    """Ring IDs whose latest case just became a Granite one.

    `seen` maps ring_id -> (version, source) from the previous refresh and is updated
    in place. A ring seen for the first time is never an upgrade, so opening the page
    does not fire a burst of notices for cases that were already there.
    """
    upgraded = []
    for ring_id, case in cases.items():
        before = seen.get(ring_id)
        if before is not None:
            old_version, old_source = before
            if case.is_granite and old_source != "granite" and case.version >= old_version:
                upgraded.append(ring_id)
        seen[ring_id] = (case.version, case.source)
    return sorted(upgraded)


def latest_case(r, ring_id: str) -> Optional[CaseView]:
    """The latest case for one ring.

    Today this scans the `cases` stream. When the case writer adds the
    `case:<ring_id>` hash (its plan, C1), this is the one function to change.
    """
    best = None
    for msg_id, f in r.xrevrange(bus.CASES, "+", "-", count=CASES_WINDOW):
        if f.get("ring_id") != ring_id:
            continue
        try:
            case = parse_case(msg_id, f)
        except Exception:
            continue
        if best is None or case_rank(case) > case_rank(best):
            best = case
    return best


def case_history(r, ring_id: str) -> List[CaseView]:
    """Every case written for one ring, oldest first: the audit trail per ring."""
    out = []
    for msg_id, f in reversed(r.xrevrange(bus.CASES, "+", "-", count=CASES_WINDOW * 4)):
        if f.get("ring_id") != ring_id:
            continue
        try:
            out.append(parse_case(msg_id, f))
        except Exception:
            continue
    return out
