"""Tests for common/splits.py (chronological train / validation / test split)."""

import json

import pandas as pd
import pytest

from common.clean_transactions import sort_and_assign_ids
from common.splits import (
    assign_split,
    build_splits_json,
    compute_cutoffs,
    split_summary,
)


def ts_from_minutes(minutes):
    return pd.Series(
        pd.to_datetime([f"2022-09-01 00:{m:02d}" for m in minutes]).astype("datetime64[ns]")
    )


def make_sorted_frame(minutes, labels=None):
    """Frame that went through sort_and_assign_ids, with a split column."""
    labels = labels if labels is not None else [0] * len(minutes)
    df = pd.DataFrame({
        "source_row": range(len(minutes)),
        "ts": ts_from_minutes(minutes),
        "label": pd.Series(labels, dtype="int8"),
    })
    df = sort_and_assign_ids(df)
    df["split"] = assign_split(df["ts"], compute_cutoffs(df["ts"]))
    return df


WORKED_MINUTES = [0, 0, 1, 2, 2, 3, 3, 3, 4, 5]


def test_worked_example():
    ts = ts_from_minutes(WORKED_MINUTES)
    cutoffs = compute_cutoffs(ts)
    assert cutoffs["validation_start"] == pd.Timestamp("2022-09-01 00:03")
    assert cutoffs["test_start"] == pd.Timestamp("2022-09-01 00:04")

    split = assign_split(ts, cutoffs)
    assert list(split.cat.categories) == ["train", "validation", "test"]
    assert split.cat.ordered
    assert split.tolist() == ["train"] * 5 + ["validation"] * 3 + ["test"] * 2


def test_no_timestamp_in_two_splits():
    df = make_sorted_frame(WORKED_MINUTES)
    splits_per_ts = df.groupby("ts", observed=True)["split"].nunique()
    assert (splits_per_ts == 1).all()


@pytest.mark.parametrize("n", [7, 13])
def test_cutoffs_taken_by_position(n):
    # Every row has its own minute, so the cutoff is exactly the row at N*6//10.
    minutes = list(range(n))
    cutoffs = compute_cutoffs(ts_from_minutes(minutes))
    assert cutoffs["validation_start"] == pd.Timestamp(f"2022-09-01 00:{n * 6 // 10:02d}")
    assert cutoffs["test_start"] == pd.Timestamp(f"2022-09-01 00:{n * 8 // 10:02d}")


def test_cutoffs_use_positional_access_with_odd_index():
    ts = ts_from_minutes(range(10))
    ts.index = range(100, 110)  # label 6 does not exist; position 6 does
    assert compute_cutoffs(ts)["validation_start"] == pd.Timestamp("2022-09-01 00:06")


def test_all_same_timestamp_raises():
    with pytest.raises(ValueError, match="train"):
        compute_cutoffs(ts_from_minutes([1] * 10))


@pytest.mark.parametrize("minutes", [[], [0], [0, 1]])
def test_fewer_than_three_rows_raises(minutes):
    with pytest.raises(ValueError, match="at least 3"):
        compute_cutoffs(ts_from_minutes(minutes))


def test_empty_validation_raises():
    # Positions 6 and 8 have the same timestamp, so validation would be empty.
    with pytest.raises(ValueError, match="validation"):
        compute_cutoffs(ts_from_minutes([0, 1, 2, 3, 4, 5, 6, 6, 6, 7]))


def test_unsorted_input_raises():
    with pytest.raises(ValueError, match="sorted"):
        compute_cutoffs(ts_from_minutes([0, 1, 2, 5, 3, 4, 6, 7, 8, 9]))


def test_labels_do_not_affect_cutoffs():
    a = make_sorted_frame(WORKED_MINUTES, labels=[0] * 10)
    b = make_sorted_frame(WORKED_MINUTES, labels=[1] * 10)
    assert compute_cutoffs(a["ts"]) == compute_cutoffs(b["ts"])
    assert a["split"].tolist() == b["split"].tolist()


def test_split_summary_counts():
    labels = [1, 0, 0, 1, 0, 1, 1, 0, 0, 1]
    df = make_sorted_frame(WORKED_MINUTES, labels=labels)
    s = split_summary(df)
    assert s["train"] == {
        "rows": 5, "positives": 2, "negatives": 3, "txn_id_min": 0, "txn_id_max": 4,
        "ts_min": "2022-09-01T00:00:00", "ts_max": "2022-09-01T00:02:00",
    }
    assert (s["validation"]["rows"], s["validation"]["positives"]) == (3, 2)
    assert (s["test"]["rows"], s["test"]["positives"], s["test"]["negatives"]) == (2, 1, 1)
    assert (s["test"]["txn_id_min"], s["test"]["txn_id_max"]) == (8, 9)


def test_build_splits_json_round_trip():
    df = make_sorted_frame(WORKED_MINUTES)
    cutoffs = compute_cutoffs(df["ts"])
    provenance = {"input_path": "HI-Small_Trans.csv", "sha256": "abc", "sample_every": None}
    doc = build_splits_json(cutoffs, split_summary(df), provenance)

    text = json.dumps(doc)
    assert json.loads(text) == doc
    assert doc["validation_start"] == "2022-09-01T00:03:00"
    assert doc["test_start"] == "2022-09-01T00:04:00"
    assert "Z" not in doc["validation_start"] + doc["test_start"]
    assert "+00:00" not in text
    assert doc["rules"]["train"] == "train: ts < validation_start"
    assert doc["rules"]["validation"] == "validation: validation_start <= ts < test_start"
    assert doc["rules"]["test"] == "test: ts >= test_start"
    assert doc["total_rows"] == 10
    assert doc["provenance"] == provenance
    assert "labels not used" in doc["method"]
