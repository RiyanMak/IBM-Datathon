"""Tests for sort_and_assign_ids in common/clean_transactions.py."""

import pandas as pd
import pytest

from common.clean_transactions import sort_and_assign_ids


def make_frame(minutes, source_rows, accounts=None):
    """Small frame with the columns that matter for ordering."""
    accounts = accounts if accounts is not None else [f"10_A{r}" for r in source_rows]
    return pd.DataFrame({
        "source_row": pd.Series(source_rows, dtype="int64"),
        "ts": pd.to_datetime([f"2022-09-01 00:{m:02d}" for m in minutes], format="ISO8601").astype("datetime64[ns]"),
        "from_acct": accounts,
        "amt_paid": [float(r) for r in source_rows],
    })


def test_output_sorted_by_ts_then_source_row():
    df = make_frame(minutes=[5, 1, 3, 1, 0], source_rows=[0, 1, 2, 3, 4])
    out = sort_and_assign_ids(df)
    assert out["source_row"].tolist() == [4, 1, 3, 2, 0]
    assert out["ts"].is_monotonic_increasing


def test_tied_timestamps_ordered_by_source_row_not_input_position():
    # Same minute everywhere, but the input lists source_row out of order.
    df = make_frame(minutes=[2, 2, 2, 2], source_rows=[7, 3, 9, 1])
    out = sort_and_assign_ids(df)
    assert out["source_row"].tolist() == [1, 3, 7, 9]


def test_txn_id_is_first_column_and_contiguous():
    df = make_frame(minutes=[4, 0, 2], source_rows=[0, 1, 2])
    out = sort_and_assign_ids(df)
    assert out.columns[0] == "txn_id"
    assert out["txn_id"].dtype == "int64"
    assert out["txn_id"].tolist() == [0, 1, 2]
    assert out.index.tolist() == [0, 1, 2]
    # Each row's other values travel with it.
    assert out["amt_paid"].tolist() == [1.0, 2.0, 0.0]


def test_duplicate_looking_rows_are_kept():
    # Two rows that are identical except for source_row are both kept.
    df = make_frame(minutes=[1, 1, 0], source_rows=[0, 1, 2], accounts=["10_X", "10_X", "10_Y"])
    df["amt_paid"] = [5.0, 5.0, 1.0]
    out = sort_and_assign_ids(df)
    assert len(out) == len(df)
    assert out["source_row"].tolist() == [2, 0, 1]


def test_row_count_unchanged_and_input_not_modified():
    df = make_frame(minutes=[3, 2, 1, 0, 3, 2], source_rows=[0, 1, 2, 3, 4, 5])
    before = df.copy()
    out = sort_and_assign_ids(df)
    assert len(out) == 6
    assert sorted(out["source_row"]) == list(range(6))
    pd.testing.assert_frame_equal(df, before)


def test_duplicate_source_row_raises():
    df = make_frame(minutes=[0, 1, 2], source_rows=[0, 1, 1])
    with pytest.raises(ValueError, match="duplicate"):
        sort_and_assign_ids(df)
