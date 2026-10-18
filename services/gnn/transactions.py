"""Load the transaction file and pull neighbourhoods out of it.

Works on the team's clean.csv (preferred: it already has the "<bank>_<account>"
IDs, amount_usd and the split column) and on the raw Kaggle HI-Small_Trans.csv
(IDs are built the same way the cleaning script builds them).

Every row comes out in the team's edge vocabulary:
    txn_id, ts (epoch seconds), from, to, amount_usd, amount_paid, ccy_paid,
    ccy_recv, fmt, label (0/1 or NaN), score (xgb, NaN when absent), split
"""
import re
from typing import Dict, Iterable, List, Optional, Set

import numpy as np
import pandas as pd

from common.data_contract import canonical_bank

# Our name -> the names other files might use for the same column.
COLUMN_ALIASES: Dict[str, List[str]] = {
    "ts":          ["ts", "timestamp", "Timestamp", "time", "datetime"],
    "from":        ["from_acct", "from", "src", "source", "sender", "src_id", "from_id"],
    "to":          ["to_acct", "to", "dst", "target", "receiver", "dst_id", "to_id"],
    "from_bank":   ["From Bank", "from_bank_raw", "from_bank", "src_bank"],
    "from_raw":    ["Account", "from_acct_raw", "from_account", "src_account"],
    "to_bank":     ["To Bank", "to_bank_raw", "to_bank", "dst_bank"],
    "to_raw":      ["Account.1", "to_acct_raw", "to_account", "dst_account"],
    "amount_usd":  ["amount_usd", "amt_usd", "usd"],
    "amount_paid": ["Amount Paid", "amt_paid", "amount_paid", "amount", "amt"],
    "ccy_paid":    ["Payment Currency", "ccy_paid", "currency_paid", "payment_currency", "currency"],
    "amount_recv": ["Amount Received", "amt_recv", "amount_received", "amount_recv"],
    "ccy_recv":    ["Receiving Currency", "ccy_recv", "currency_recv", "receiving_currency"],
    "fmt":         ["Payment Format", "fmt", "format", "payment_format"],
    "label":       ["Is Laundering", "label", "is_laundering", "y"],
    "score":       ["score", "xgb_score", "risk", "risk_score", "p_xgb"],
    "txn_id":      ["txn_id", "id", "tx_id", "transaction_id"],
    "split":       ["split"],
}

OUTPUT_COLUMNS = ["txn_id", "ts", "from", "to", "amount_usd", "amount_paid", "ccy_paid",
                  "ccy_recv", "fmt", "label", "score", "split"]

_ID_COLUMNS = {n for k in ("from", "to", "from_bank", "from_raw", "to_bank", "to_raw", "txn_id")
               for n in COLUMN_ALIASES[k]}


def _find(df: pd.DataFrame, ours: str) -> Optional[str]:
    for name in COLUMN_ALIASES[ours]:
        if name in df.columns:
            return name
    return None


# Naive, not tz-aware: building a timezone-aware index segfaults in pandas on s390x,
# and every timestamp in this project is UTC-naive text anyway.
_EPOCH = pd.Timestamp(0)


def _to_epoch_seconds(series: pd.Series) -> np.ndarray:
    # pandas.api.types, not np.issubdtype: pandas 3 reads text as StringDtype,
    # which numpy cannot interpret.
    if pd.api.types.is_numeric_dtype(series):
        vals = series.astype("float64").to_numpy()
        if np.isnan(vals).any():
            raise ValueError("timestamp column has missing values")
        return vals / 1000.0 if np.median(vals) > 4e10 else vals   # milliseconds?
    if pd.api.types.is_datetime64_any_dtype(series):
        dt = pd.to_datetime(series)
        if getattr(dt.dt, "tz", None) is not None:
            dt = dt.dt.tz_convert("UTC").dt.tz_localize(None)
    else:
        s = series.astype(str).str.strip().str.replace("Z", "", regex=False)
        first = s.iloc[0] if len(s) else ""
        if re.match(r"^\d{4}/\d{2}/\d{2} ", first):     # raw Kaggle file
            # pandas' strptime-style parsing (format="%Y/%m/%d %H:%M") segfaults on
            # s390x; the ISO parser does not, so make the text ISO first.
            s = s.str.replace("/", "-", regex=False)
        # Covers the raw file (after the replace), clean.csv's "2022-09-01T00:20:00"
        # and the "2022-09-01" midnights that older clean.csv files have.
        dt = pd.to_datetime(s, format="ISO8601", errors="coerce")
        bad = dt.isna()
        if bad.any():
            dt = dt.copy()
            dt[bad] = pd.to_datetime(s[bad], errors="coerce", format="mixed")
    bad = dt.isna()
    if bad.any():
        examples = series[bad.to_numpy()].head(3).tolist()
        raise ValueError(f"{int(bad.sum())} timestamps could not be parsed, e.g. {examples}")
    # Subtracting the epoch works at any resolution; pandas 3 parses text at
    # microseconds, so casting to int64 and dividing by 1e9 would be 1000x off.
    return ((dt - _EPOCH) / pd.Timedelta(seconds=1)).to_numpy().astype("float64")


def load_transactions(path: str, nrows: Optional[int] = None) -> pd.DataFrame:
    """Read clean.csv or the raw Kaggle CSV. Returns OUTPUT_COLUMNS, sorted by time."""
    if path.endswith(".parquet") or path.endswith(".pq"):
        df = pd.read_parquet(path)
        if nrows:
            df = df.head(nrows)
    else:
        header = list(pd.read_csv(path, nrows=0).columns)
        dtype = {c: str for c in header if c in _ID_COLUMNS}
        df = pd.read_csv(path, nrows=nrows, low_memory=False, dtype=dtype, na_filter=False)

    out = pd.DataFrame()

    ts_col = _find(df, "ts")
    if ts_col is None:
        raise ValueError(f"No timestamp column found. Columns are: {list(df.columns)}")
    out["ts"] = _to_epoch_seconds(df[ts_col])

    # Account IDs: already combined (clean.csv) or bank + account (raw file).
    f_col, t_col = _find(df, "from"), _find(df, "to")
    if f_col and t_col:
        out["from"] = df[f_col].astype(str)
        out["to"] = df[t_col].astype(str)
    else:
        fb, fr, tb, tr = (_find(df, k) for k in ("from_bank", "from_raw", "to_bank", "to_raw"))
        if not (fr and tr):
            raise ValueError(f"Cannot find account columns. Columns are: {list(df.columns)}")
        fbank = df[fb].map(canonical_bank) if fb else ""
        tbank = df[tb].map(canonical_bank) if tb else ""
        out["from"] = (fbank + "_" if fb else "") + df[fr].astype(str)
        out["to"] = (tbank + "_" if tb else "") + df[tr].astype(str)

    ap, au = _find(df, "amount_paid"), _find(df, "amount_usd")
    if ap is None and au is None:
        raise ValueError(f"No amount column found. Columns are: {list(df.columns)}")
    out["amount_paid"] = pd.to_numeric(df[ap if ap else au], errors="coerce").fillna(0.0)
    out["amount_usd"] = pd.to_numeric(df[au], errors="coerce").fillna(out["amount_paid"]) if au else out["amount_paid"]

    cp, cr = _find(df, "ccy_paid"), _find(df, "ccy_recv")
    out["ccy_paid"] = df[cp].astype(str) if cp else "US Dollar"
    out["ccy_recv"] = df[cr].astype(str) if cr else out["ccy_paid"]

    fm = _find(df, "fmt")
    out["fmt"] = df[fm].astype(str) if fm else "ACH"

    lb = _find(df, "label")
    out["label"] = pd.to_numeric(df[lb], errors="coerce") if lb else np.nan

    sc = _find(df, "score")
    out["score"] = pd.to_numeric(df[sc], errors="coerce") if sc else np.nan

    sp = _find(df, "split")
    out["split"] = df[sp].astype(str) if sp else ""

    tid = _find(df, "txn_id")
    if tid:
        out["txn_id"] = df[tid].astype(str)

    # Stable sort = by (ts, original row), the same order the cleaning script uses.
    out = out.sort_values("ts", kind="stable").reset_index(drop=True)
    if not tid:
        # Position after that sort, like common/clean_transactions.sort_and_assign_ids.
        out["txn_id"] = [str(i) for i in range(len(out))]
    return out[OUTPUT_COLUMNS]


def time_split(df: pd.DataFrame, train=0.6, val=0.2):
    """(train_df, val_df, test_df). Uses the clean.csv split column when it is
    there, otherwise cuts by position in time order. Never shuffles."""
    if (df["split"] != "").all() and set(df["split"].unique()) <= {"train", "validation", "test"}:
        return (df[df["split"] == "train"], df[df["split"] == "validation"], df[df["split"] == "test"])
    n = len(df)
    a, b = int(n * train), int(n * (train + val))
    return df.iloc[:a], df.iloc[a:b], df.iloc[b:]


class TxnIndex:
    """Fast 'which transfers touch this account?' lookups over a DataFrame."""

    def __init__(self, df: pd.DataFrame):
        self.df = df.reset_index(drop=True)
        self.ts = self.df["ts"].to_numpy()
        self._from = self.df["from"].to_numpy()
        self._to = self.df["to"].to_numpy()
        self.by_from = self.df.groupby("from").indices   # account -> np.array of row numbers
        self.by_to = self.df.groupby("to").indices
        self.by_txn = {t: i for i, t in enumerate(self.df["txn_id"].to_numpy())}

    def rows_for_account(self, acct: str) -> np.ndarray:
        a = self.by_from.get(acct, np.empty(0, dtype=np.int64))
        b = self.by_to.get(acct, np.empty(0, dtype=np.int64))
        return np.concatenate([a, b]) if len(a) or len(b) else np.empty(0, dtype=np.int64)

    def neighborhood(self, accounts: Iterable[str], hops: int = 2,
                     t_lo: Optional[float] = None, t_hi: Optional[float] = None,
                     max_edges: int = 3000, rng: Optional[np.random.Generator] = None) -> np.ndarray:
        """Row numbers of every transfer within `hops` of `accounts`, inside the
        time window [t_lo, t_hi]. Capped at max_edges (hop-1 edges kept first)."""
        rng = rng or np.random.default_rng(0)
        frontier: Set[str] = set(map(str, accounts))
        seen: Set[str] = set(frontier)
        chosen: List[np.ndarray] = []
        budget = max_edges
        for _ in range(hops):
            rows = [self.rows_for_account(a) for a in frontier]
            rows = np.unique(np.concatenate(rows)) if rows else np.empty(0, dtype=np.int64)
            if t_lo is not None:
                rows = rows[self.ts[rows] >= t_lo]
            if t_hi is not None:
                rows = rows[self.ts[rows] <= t_hi]
            if len(rows) > budget:
                rows = rng.choice(rows, size=budget, replace=False)
            chosen.append(rows)
            budget -= len(rows)
            if budget <= 0:
                break
            nxt = set(self._from[rows]) | set(self._to[rows])
            frontier = nxt - seen
            seen |= nxt
            if not frontier:
                break
        if not chosen:
            return np.empty(0, dtype=np.int64)
        return np.unique(np.concatenate(chosen))

    def rows_to_transfers(self, rows: np.ndarray) -> List[dict]:
        recs = self.df.iloc[rows].to_dict("records")
        for r in recs:
            if pd.isna(r.get("label")):
                r["label"] = None
            if pd.isna(r.get("score")):
                r["score"] = None
        return recs
