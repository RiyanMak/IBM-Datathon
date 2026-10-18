"""The case packet download: one Markdown file per case, for both page front ends. No Streamlit.

    from services.page import packet_export
    found = packet_export.build(r, "R-0042", version=3)
    if found:
        name, markdown = found        # "case_R-0042_v3.md", "# Case packet: R-0042, version 3 ..."

The notifier already knows how to write the packet (services/notifier/packet.py); this
module only gathers its three inputs from Redis: the `cases` message, the matching
`rings` message (the accounts and transfers, which a case does not carry) and the
`decisions` messages for the ring. It reads raw stream fields, not the page's views,
because the packet renderer works from the same text the streams carry.

Reads use the same bounded XREVRANGE windows as state.py, so a download costs no more
than one page refresh. Never raises on bad data: a broken message is skipped and logged,
and a Redis error gives "no packet" rather than a crash.
"""
import logging
from typing import Dict, List, Optional, Tuple

from common import bus
from services.notifier import packet
from services.page import state

log = logging.getLogger(__name__)

Fields = Dict[str, str]


def _text(value):
    """Redis may hand back bytes when the client does not decode; make it text."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def _fields(raw) -> Fields:
    """One message's fields with text keys and values; {} for anything that is not a dict."""
    if not isinstance(raw, dict):
        return {}
    return {_text(k): _text(v) for k, v in raw.items()}


def _messages(r, stream: str, ring_id: str, count: int) -> List[Tuple[str, Fields]]:
    """(message id, fields) for one ring, newest first, from the last `count` messages."""
    out = []
    for msg_id, raw in r.xrevrange(stream, "+", "-", count=count):
        f = _fields(raw)
        if f.get("ring_id") == ring_id:
            out.append((str(_text(msg_id)), f))
    return out


def _version(f: Fields) -> Optional[int]:
    v = state.to_float(f.get("version"))
    return int(v) if v is not None else None


def _pick_case(rows, version) -> Optional[Fields]:
    """The case the packet describes: among those for `version` (when there are any), the
    one state.case_rank prefers (a Granite case beats a template at the same version);
    otherwise the best case overall, which is the newest version."""
    ranked = []
    for msg_id, f in rows:
        try:
            ranked.append((state.case_rank(state.parse_case(msg_id, f)), f))
        except Exception as exc:
            log.warning("case packet: skipped bad cases message %s: %s", msg_id, exc)
    if not ranked:
        return None
    if version is not None:
        same = [item for item in ranked if item[0][0] == version]
        ranked = same or ranked
    return max(ranked, key=lambda item: item[0])[1]


def _pick_ring(rows, version) -> Optional[Fields]:
    """The rings message for `version` if one is in the window, else the newest for the ring."""
    if not rows:
        return None
    if version is not None:
        for _msg_id, f in rows:   # newest first, so the first match is the latest send
            if _version(f) == version:
                return f
    return max(rows, key=lambda row: (_version(row[1]) or 0, state.stream_id_ms(row[0])))[1]


def fetch(r, ring_id: str, version=None) -> Tuple[Optional[Fields], Optional[Fields], List[Fields]]:
    """(case fields, ring fields, decision fields oldest first) for one ring, as raw text.

    `version` is the version the investigator is reviewing. The case and ring for that
    version are preferred; when the case writer has not caught up, the newest case is
    used and the packet header says which version it describes. Missing pieces come back
    as None or []; this never raises.
    """
    ring_id = str(ring_id or "").strip()
    wanted = state.to_float(version)
    wanted = int(wanted) if wanted is not None else None
    case = ring = None
    decisions: List[Fields] = []
    if not ring_id:
        return None, None, []
    try:
        case = _pick_case(_messages(r, bus.CASES, ring_id, state.CASES_WINDOW), wanted)
        if case is not None:
            # The transfers must be the ones the case describes, so match the case's version.
            ring = _pick_ring(_messages(r, bus.RINGS, ring_id, state.RINGS_WINDOW), _version(case))
            decisions = [f for _id, f in reversed(
                _messages(r, bus.DECISIONS, ring_id, state.DECISIONS_WINDOW))]
    except Exception as exc:
        log.warning("case packet for %s: could not read Redis: %s", ring_id, exc)
    return case, ring, decisions


def build(r, ring_id: str, version=None) -> Optional[Tuple[str, str]]:
    """(filename, Markdown) for one ring's case packet, or None when there is no case yet.

    A ring without a case has no summary or facts to file, so the page shows an
    explanation instead of a button. The text is a draft for a person to review and file.
    """
    case, ring, decisions = fetch(r, ring_id, version)
    if case is None:
        return None
    try:
        return packet.filename(case), packet.render(case, ring, decisions, manifest=packet.load_manifest())
    except Exception:   # packet.render promises not to raise; this is the belt to its braces
        log.exception("case packet for %s could not be rendered", ring_id)
        return None
