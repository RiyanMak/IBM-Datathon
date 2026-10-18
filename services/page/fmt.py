"""Display formatting shared by the Streamlit page and a possible Flask fallback.
No Streamlit. Every function accepts junk and returns something printable."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from services.page.models import Counters
from services.page.state import wall_seconds

DASH = "–"


def count(n) -> str:
    """12345 -> '12,345'."""
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return DASH


def ms(x: Optional[float]) -> str:
    """Latency in milliseconds: 4.96 -> '5.0 ms', 123.4 -> '123 ms', None -> '–'."""
    if x is None:
        return DASH
    return ("%.1f ms" if x < 10 else "%.0f ms") % x


# The case writer's facts pack, in reading order. Keys the header already shows
# (scores, shape, account count, time window) are left out. Unknown keys still show,
# after these, with a readable label, so a new fact needs no page change.
FACT_LABELS = {
    "key_account": "Key account",
    "key_received_usd": "Key account received",
    "key_sent_usd": "Key account sent on",
    "passed_on_pct": "Share passed on",
    "hours_to_pass_on": "Began passing on after",
    "hours_to_pass_on_all": "Had passed it all on after",
    "key_senders": "Key account's senders",
    "key_payees": "Key account's payees",
    "origin": "Origin",
    "origin_sent_usd": "Origin sent",
    "collector": "Collector",
    "collector_received_usd": "Collector received",
    "n_transfers": "Transfers",
    "duration_hours": "Duration",
    "min_transfer_usd": "Smallest transfer",
    "max_transfer_usd": "Largest transfer",
    "n_just_under_threshold": "Just under the reporting threshold",
    "reporting_threshold_usd": "Reporting threshold",
}
FACT_SKIP = {"ring_id", "shape", "n_accounts", "first_seen", "last_seen", "xgb_score",
             "gnn_score", "total_usd", "version", "label", "is_laundering",
             "ground_truth", "pattern_id", "pattern_membership", "evaluation_label"}


def fact_value(key: str, value) -> Optional[str]:
    """One fact as display text; None when there is nothing worth showing."""
    if value is None or value == "" or isinstance(value, (list, dict)):
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key.endswith("_usd"):
        try:
            return "${:,.0f}".format(float(value))
        except (TypeError, ValueError):
            return str(value)
    if key.endswith("_pct"):
        try:
            return "%g%%" % float(value)
        except (TypeError, ValueError):
            return str(value)
    if "hours" in key:
        try:
            return "%.1f h" % float(value)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, float):
        return "%g" % value
    return str(value)


def fact_rows(facts: Optional[dict]) -> List[tuple]:
    """(label, value) pairs for the facts panel: known facts first, then any new ones."""
    if not facts:
        return []
    from services.page.models import readable
    keys = [k for k in FACT_LABELS if k in facts]
    keys += sorted(k for k in facts if k not in FACT_LABELS and k not in FACT_SKIP)
    rows = []
    for k in keys:
        text = fact_value(k, facts[k])
        if text is not None:
            rows.append((FACT_LABELS.get(k, readable(k)), text))
    return rows


@dataclass
class Tile:
    label: str
    value: str
    help: str


def top_bar(c: Counters) -> List[Tile]:
    """The top bar's tiles, in order. Labels say when a number is only recent."""
    if c.counts_exact:
        pct = (100.0 * c.flagged / c.scored_total) if c.scored_total else 0.0
        flagged = Tile("Flagged", count(c.flagged),
                       "%.2f%% of all scored transfers, from the scorer's own counter." % pct)
    else:
        flagged = Tile("Flagged, last %s" % count(c.flagged_window), count(c.flagged),
                       "Counted over the most recent %s scored transfers. Exact totals need the "
                       "scorer's stats:scorer counter." % count(c.flagged_window))
    window = ("over the last %s scored transfers" % count(c.latency_samples)
              if c.latency_samples else "no scored transfers yet")
    granite_help = ("From the case writer's own counter." if c.granite_from_stats else
                    "Counted from recent cases. A case counts as tried when Granite passed, "
                    "was rejected by the fact check, or failed.")
    return [
        Tile("Transfers scored", count(c.scored_total),
             "From the scorer's own counter." if c.counts_exact else
             "Messages on the scored stream. Undercounts once the stream is capped."),
        flagged,
        Tile("Open rings", "%s of %s" % (count(c.open_rings), count(c.total_rings)),
             "Rings without a decision, out of the rings currently listed."),
        Tile("Latency p50", ms(c.p50_ms), "Time from sent to scored, median, %s." % window),
        Tile("Latency p99", ms(c.p99_ms), "Time from sent to scored, 99th percentile, %s." % window),
        Tile("Granite passed fact check",
             "%s of %s" % (count(c.granite_passed), count(c.granite_tried)) if c.granite_tried else DASH,
             granite_help),
    ]


def score(x: Optional[float]) -> str:
    """0.931 -> '0.93'; None -> '–' (a missing GNN score is not a zero)."""
    return DASH if x is None else "%.2f" % x


def data_time(text: str) -> str:
    """Data time as sent ('2022-09-08T14:00:00') -> '2022-09-08 14:00'."""
    if not text:
        return DASH
    return text.replace("T", " ")[:16]


def span_hours(first: str, last: str) -> str:
    """Hours between two data times -> '4.7 h'; '' when either is unreadable."""
    try:
        a = datetime.fromisoformat(first)
        b = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return ""
    hours = (b - a).total_seconds() / 3600.0
    return "%.1f h" % hours if hours >= 0 else ""


def wall_time(text: str) -> str:
    """Wall-clock time -> '2026-10-17 10:42 UTC'. A time written without a zone
    (the case writer's local written_at) is shown without one rather than mislabelled.
    Unreadable text comes back as it is."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    secs = wall_seconds(text)
    if secs is None:
        return text or DASH
    return datetime.fromtimestamp(secs, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


_MD_SPECIAL = set("\\`*_{}[]<>#|$~")


def md(text) -> str:
    """Data text made safe for st.markdown, captions, radio labels and badges.

    Streamlit reads '$...$' as a maths formula, so a summary with two dollar amounts
    turns into garbled maths. Escaping every Markdown special character means text
    from Redis (summaries, reasons, notes, IDs) always shows exactly as written.
    """
    out = []
    for ch in str(text):
        out.append("\\" + ch if ch in _MD_SPECIAL else ch)
    return "".join(out)
