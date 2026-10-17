"""Display formatting shared by the Streamlit page and a possible Flask fallback.
No Streamlit. Every function accepts junk and returns something printable."""
from datetime import datetime, timezone
from typing import Optional

from services.page.state import wall_seconds

DASH = "–"


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
