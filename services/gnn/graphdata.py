"""Turn a list of transfers into the tensors the GNN needs.

A "transfer" here is a plain dict in the team's edge vocabulary:

    {"txn_id": "4100001", "from": "27_SRC0001A0", "to": "10_8000EBD30",
     "amount_usd": 10000.0, "ts": "2022-09-08T14:00:00", "score": 0.71}

plus, when they are known (from clean.csv), "ccy_paid", "ccy_recv", "fmt"
and "label". Account IDs are "<bank>_<account>" as in clean.csv. Only
equality matters for the graph; the bank part is used for one feature.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import torch

# Order matters: the model is trained on these exact columns.
EDGE_FEATURES = [
    "log_amount",        # log(1 + amount in USD)
    "cur_mismatch",      # 1 if paid and received currencies differ
    "cross_bank",        # 1 if sender's bank != receiver's bank
    "hour_sin",          # time of day, encoded as a circle
    "hour_cos",
    "fmt_ach",           # payment format one-hots
    "fmt_cash",
    "fmt_other",
    "xgb_score",         # tier-1 score ("score" on the edge); 0.5 when missing
    "hours_since_start", # hours since the earliest transfer in this subgraph
    "round_amount",      # 1 if amount is a multiple of 100
]

NODE_FEATURES = [
    "log_in_count", "log_out_count",
    "log_in_amount", "log_out_amount",
    "log_in_distinct", "log_out_distinct",
    "passthrough",       # out_amount / in_amount, clipped to [0, 2]
    "log_degree",
]


def parse_ts(value) -> float:
    """Seconds since epoch for '2022-09-08T14:00:00', '2022/09/08 14:00', or a number."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):             # datetime / pandas Timestamp
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return float(value.timestamp())
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_iso(seconds: float) -> str:
    """Epoch seconds -> the team's data-time text '2022-09-08T14:00:00'."""
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def bank_of(account_id: str) -> str:
    """'10_8000EBD30' -> '10'. Also accepts the older '010:8000EBD30' form."""
    s = str(account_id)
    for sep in ("_", ":"):
        if sep in s:
            return s.split(sep, 1)[0]
    return ""


def edge_src(e: dict) -> str:
    return str(e.get("from") if e.get("from") is not None else e.get("src"))


def edge_dst(e: dict) -> str:
    return str(e.get("to") if e.get("to") is not None else e.get("dst"))


def edge_amount(e: dict) -> float:
    for k in ("amount_usd", "amount_paid", "amt_paid", "amount"):
        if e.get(k) is not None:
            return float(e[k])
    return 0.0


def edge_score(e: dict) -> Optional[float]:
    for k in ("score", "xgb_score"):
        v = e.get(k)
        if v is not None and v != "":
            return float(v)
    return None


@dataclass
class SubGraph:
    node_ids: List[str]                 # index -> account id
    node_index: Dict[str, int]          # account id -> index
    x: torch.Tensor                     # [N, len(NODE_FEATURES)]
    edge_index: torch.Tensor            # [2, E]  (sender row, receiver row)
    edge_attr: torch.Tensor             # [E, len(EDGE_FEATURES)]
    txn_ids: List[str]                  # index -> txn id
    labels: Optional[torch.Tensor] = None   # [E] floats 0/1, if known
    meta: dict = field(default_factory=dict)

    @property
    def num_nodes(self):
        return len(self.node_ids)

    @property
    def num_edges(self):
        return len(self.txn_ids)


def _fmt_onehot(fmt) -> tuple:
    f = str(fmt or "").strip().lower()
    if f == "ach":
        return (1.0, 0.0, 0.0)
    if f == "cash":
        return (0.0, 1.0, 0.0)
    return (0.0, 0.0, 1.0)


def build_subgraph(transfers: List[dict]) -> SubGraph:
    """Build node and edge tensors from a list of transfer dicts."""
    if not transfers:
        raise ValueError("build_subgraph needs at least one transfer")

    node_index: Dict[str, int] = {}
    node_ids: List[str] = []

    def idx(acct) -> int:
        a = str(acct)
        if a not in node_index:
            node_index[a] = len(node_ids)
            node_ids.append(a)
        return node_index[a]

    ts = np.array([parse_ts(t.get("ts")) for t in transfers], dtype=np.float64)
    t0 = float(ts.min())

    src_rows, dst_rows, feats, txn_ids, labels = [], [], [], [], []
    have_labels = all(t.get("label") is not None for t in transfers)

    for t, tsec in zip(transfers, ts):
        s_id, d_id = edge_src(t), edge_dst(t)
        s, d = idx(s_id), idx(d_id)
        amount = edge_amount(t)
        cur_p = str(t.get("ccy_paid") or t.get("currency_paid") or "")
        cur_r = str(t.get("ccy_recv") or t.get("currency_recv") or cur_p)
        dt = datetime.fromtimestamp(tsec, tz=timezone.utc)
        hour = dt.hour + dt.minute / 60.0
        ach, cash, other = _fmt_onehot(t.get("fmt") or t.get("format"))
        xgb = edge_score(t)
        xgb = 0.5 if xgb is None else xgb
        bs, bd = bank_of(s_id), bank_of(d_id)
        feats.append([
            math.log1p(max(amount, 0.0)),
            1.0 if cur_p != cur_r else 0.0,
            1.0 if (bs and bd and bs != bd) else 0.0,
            math.sin(2 * math.pi * hour / 24.0),
            math.cos(2 * math.pi * hour / 24.0),
            ach, cash, other,
            xgb,
            (tsec - t0) / 3600.0,
            1.0 if amount > 0 and abs(amount / 100.0 - round(amount / 100.0)) < 1e-9 else 0.0,
        ])
        src_rows.append(s)
        dst_rows.append(d)
        txn_ids.append(str(t.get("txn_id", f"tx-{len(txn_ids)}")))
        if have_labels:
            labels.append(float(t["label"]))

    n = len(node_ids)
    in_count = np.zeros(n); out_count = np.zeros(n)
    in_amt = np.zeros(n); out_amt = np.zeros(n)
    in_src = [set() for _ in range(n)]; out_dst = [set() for _ in range(n)]
    for t, s, d in zip(transfers, src_rows, dst_rows):
        amount = edge_amount(t)
        out_count[s] += 1; in_count[d] += 1
        out_amt[s] += amount; in_amt[d] += amount
        out_dst[s].add(d); in_src[d].add(s)

    passthrough = np.where(in_amt > 0, out_amt / np.maximum(in_amt, 1e-9), 0.0)
    passthrough = np.clip(passthrough, 0.0, 2.0)
    x = np.stack([
        np.log1p(in_count), np.log1p(out_count),
        np.log1p(in_amt), np.log1p(out_amt),
        np.log1p([len(s) for s in in_src]), np.log1p([len(s) for s in out_dst]),
        passthrough,
        np.log1p(in_count + out_count),
    ], axis=1).astype(np.float32)

    edge_attr = torch.tensor(np.array(feats, dtype=np.float32))
    # Scale the two unbounded columns so they sit in a friendly range.
    edge_attr[:, 0] = edge_attr[:, 0] / 10.0           # log amount: ~0..2
    edge_attr[:, 9] = edge_attr[:, 9] / (24.0 * 7.0)   # hours: 0..~1 for a 7-day window

    return SubGraph(
        node_ids=node_ids,
        node_index=node_index,
        x=torch.tensor(x),
        edge_index=torch.tensor([src_rows, dst_rows], dtype=torch.long),
        edge_attr=edge_attr,
        txn_ids=txn_ids,
        labels=torch.tensor(labels, dtype=torch.float32) if have_labels else None,
        meta={"t0": t0},
    )
