"""Chronological train / validation / test split of the sorted transactions.

The split is decided by time only, never by labels:

    validation_start = timestamp at zero-based sorted position floor(0.60 * N)
    test_start       = timestamp at zero-based sorted position floor(0.80 * N)

    train:      ts <  validation_start
    validation: validation_start <= ts < test_start
    test:       ts >= test_start

Because the rules compare timestamps, all rows with the same timestamp always
end up in the same split. The split sizes are therefore close to, but not
exactly, 60 / 20 / 20.
"""

import numpy as np
import pandas as pd

from common.data_contract import SPLIT_NAMES

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"

INTERVAL_RULES = {
    "train": "train: ts < validation_start",
    "validation": "validation: validation_start <= ts < test_start",
    "test": "test: ts >= test_start",
}

METHOD_TEXT = (
    "timestamps at zero-based sorted positions floor(0.60*N) and floor(0.80*N); "
    "whole timestamp groups stay in one split; labels not used"
)

SPLIT_DTYPE = pd.CategoricalDtype(categories=list(SPLIT_NAMES), ordered=True)


def to_iso(ts) -> str:
    """Timestamp as ISO text "YYYY-MM-DDTHH:MM:SS" (no timezone, no "Z")."""
    return pd.Timestamp(ts).strftime(ISO_FORMAT)


def compute_cutoffs(ts_sorted: pd.Series) -> dict:
    """Return {"validation_start": Timestamp, "test_start": Timestamp}.

    ts_sorted must already be sorted ascending (e.g. the ts column after
    sort_and_assign_ids). Raises ValueError if it is not sorted, has missing
    values, or if any of the three splits would be empty.
    """
    n = len(ts_sorted)
    if n < 3:
        raise ValueError(f"need at least 3 rows to make three splits, got {n}")
    if ts_sorted.isna().any():
        raise ValueError("ts contains missing values")
    if not ts_sorted.is_monotonic_increasing:
        raise ValueError("ts must be sorted ascending before computing cutoffs")

    # Integer arithmetic, so there is no floating point rounding.
    validation_start = pd.Timestamp(ts_sorted.iloc[n * 6 // 10])
    test_start = pd.Timestamp(ts_sorted.iloc[n * 8 // 10])
    first_ts = pd.Timestamp(ts_sorted.iloc[0])

    if validation_start == first_ts:
        raise ValueError(
            f"train split would be empty: validation_start {validation_start} "
            f"is the earliest timestamp"
        )
    if validation_start == test_start:
        raise ValueError(
            f"validation split would be empty: validation_start and test_start "
            f"are both {validation_start}"
        )
    # The test split always contains the row at position n*8//10, so it can
    # never be empty.
    return {"validation_start": validation_start, "test_start": test_start}


def assign_split(ts: pd.Series, cutoffs: dict) -> pd.Series:
    """Label every timestamp with "train", "validation" or "test".

    Returns an ordered categorical Series with the same index as ts.
    """
    validation_start = cutoffs["validation_start"]
    test_start = cutoffs["test_start"]
    values = np.select(
        [ts < validation_start, ts < test_start],
        ["train", "validation"],
        default="test",
    )
    return pd.Series(pd.Categorical(values, dtype=SPLIT_DTYPE), index=ts.index, name="split")


def split_summary(df: pd.DataFrame) -> dict:
    """Row counts, label counts, txn_id range and time range for each split.

    df needs the columns split, label, txn_id and ts.
    """
    summary = {}
    for name in SPLIT_NAMES:
        part = df[df["split"] == name]
        positives = int((part["label"] == 1).sum())
        empty = len(part) == 0
        summary[name] = {
            "rows": int(len(part)),
            "positives": positives,
            "negatives": int(len(part)) - positives,
            "txn_id_min": None if empty else int(part["txn_id"].min()),
            "txn_id_max": None if empty else int(part["txn_id"].max()),
            "ts_min": None if empty else to_iso(part["ts"].min()),
            "ts_max": None if empty else to_iso(part["ts"].max()),
        }
    return summary


def build_splits_json(cutoffs: dict, summary: dict, provenance: dict) -> dict:
    """Build the content of splits.json (plain Python types only)."""
    return {
        "validation_start": to_iso(cutoffs["validation_start"]),
        "test_start": to_iso(cutoffs["test_start"]),
        "rules": dict(INTERVAL_RULES),
        "method": METHOD_TEXT,
        "total_rows": int(sum(s["rows"] for s in summary.values())),
        "summary": summary,
        "provenance": dict(provenance),
    }
