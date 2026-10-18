"""Plain data the page shows. No Streamlit, no Redis: the Flask fallback reuses these.

Everything here is already parsed and cleaned. A view never has to call json.loads
or guess whether a score is missing.
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


# --- label registries -------------------------------------------------------
# Unknown values are never an error: they fall back to a readable form of the raw
# value, so a new shape, source or summary part shows up without a page change.

SHAPE_LABELS = {
    "fan-out": "Fan-out",
    "fan-in": "Fan-in",
    "scatter-gather": "Scatter-gather",
    "gather-scatter": "Gather-scatter",
    "cycle": "Cycle",
    "bipartite": "Bipartite",
    "stack": "Stack",
    "other": "Other",
}

SOURCE_LABELS = {
    "granite": "Granite explanation",
    "template": "Template",
}

PART_LABELS = {
    "summary": "Summary",
    "what_happened": "What happened",
    "why_suspicious": "Why it is suspicious",
    "possible_victim": "Possible victim",
    "suggested_next_step": "Suggested next step",
}

DECISION_LABELS = {
    "freeze": "Freeze recorded",
    "dismiss": "Dismissed",
    "more_info": "Needs more info",
}
DECISIONS = tuple(DECISION_LABELS)


def normalise_shape(raw: str) -> str:
    """'CYCLE', 'Scatter gather', 'fan_out' -> 'cycle', 'scatter-gather', 'fan-out'."""
    key = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    return key or "other"


def readable(key: str) -> str:
    """Fallback label: 'draft_report' -> 'Draft report'."""
    text = (key or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def shape_label(key: str) -> str:
    return SHAPE_LABELS.get(key, readable(key))


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "Unknown source")


def part_label(key: str) -> str:
    return PART_LABELS.get(key, readable(key))


def decision_label(decision: str) -> str:
    return DECISION_LABELS.get(decision, readable(decision))


# --- views ------------------------------------------------------------------

@dataclass
class Edge:
    txn_id: str
    src: str
    dst: str
    amount_usd: Optional[float]
    ts: str              # data time, as sent
    score: Optional[float]
    amt_paid: Optional[float] = None
    ccy_paid: str = ""
    amt_recv: Optional[float] = None
    ccy_recv: str = ""


@dataclass
class RingView:
    ring_id: str
    version: int
    shape: str           # normalised key, e.g. "scatter-gather"
    n_accounts: int
    accounts: List[str]
    edges: List[Edge]
    xgb_score: Optional[float]
    gnn_score: Optional[float]   # None until the GNN runs
    reasons: List[str]
    first_seen: str      # data time
    last_seen: str       # data time
    received_ms: int     # wall clock, from the Redis stream ID

    @property
    def shape_label(self) -> str:
        return shape_label(self.shape)

    @property
    def total_usd(self) -> float:
        return sum(e.amount_usd or 0.0 for e in self.edges)


@dataclass
class SummaryPart:
    key: str
    label: str
    text: str


@dataclass
class CaseView:
    ring_id: str
    version: int
    parts: List[SummaryPart]
    source: str
    check_note: str
    written_at: str      # wall clock, as the case writer wrote it
    facts: Optional[dict]
    received_ms: int     # wall clock, from the Redis stream ID

    @property
    def source_label(self) -> str:
        return source_label(self.source)

    @property
    def is_granite(self) -> bool:
        return self.source == "granite"

    @property
    def reused(self) -> bool:
        """The case writer re-sent an earlier version's summary because only the clock moved."""
        return self.check_note.strip().lower().startswith("reused v")

    @property
    def fact_checked(self) -> bool:
        # a reused Granite summary passed the check when it was first written
        return self.is_granite and (self.check_note.strip().lower() == "passed fact check" or self.reused)


@dataclass
class DecisionView:
    ring_id: str
    version: Optional[int]   # extra field; old decisions may not carry it
    decision: str
    note: str
    decided_at: str
    channel: str = ""        # "page" or "slack"; older decisions carry none
    decided_by: str = ""     # who decided, e.g. "slack:U0123ABC:jane.doe"; empty from the page

    @property
    def label(self) -> str:
        return decision_label(self.decision)


@dataclass
class Counters:
    scored_total: int = 0            # XLEN scored, or the scorer's own counter
    flagged: int = 0
    flagged_window: int = 0          # how many scored rows `flagged` was counted over
    counts_exact: bool = False       # True when the scorer's stats:scorer hash exists
    p50_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    latency_samples: int = 0
    total_rings: int = 0
    open_rings: int = 0
    granite_passed: int = 0
    granite_tried: int = 0
    granite_from_stats: bool = False  # True when stats:casewriter exists


@dataclass
class PageState:
    counters: Counters = field(default_factory=Counters)
    rings: List[RingView] = field(default_factory=list)          # latest version per ring
    cases: Dict[str, CaseView] = field(default_factory=dict)     # latest case per ring
    decisions: Dict[str, DecisionView] = field(default_factory=dict)  # latest decision per ring
    redis_ok: bool = True
    errors: List[str] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)         # bad messages per stream
    loaded_ms: int = 0
    feed_mode: str = "synthetic"
    last_event_ms: int = 0

    def ring(self, ring_id: str) -> Optional[RingView]:
        return next((r for r in self.rings if r.ring_id == ring_id), None)

    def to_dict(self) -> dict:
        """JSON-ready form, for the Flask fallback's /api/state."""
        return asdict(self)
