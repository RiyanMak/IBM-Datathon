"""Tests for the metric helpers of scripts/train_xgboost.py and the taint walk of scripts/train_taint.py."""

import numpy as np
import pytest

from common.features import DAY
from scripts.train_taint import TAINT_NAMES, taint_clues
from scripts.train_xgboost import SPLIT_DATE, choose_thresholds, metrics, split_tables


def test_metrics_counts_and_precision_at_k():
    y = np.array([1, 0, 1, 0, 0, 1, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.2, 0.1, 0.05, 0.3, 0.0])
    m = metrics(y, p, threshold=0.5)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 4)
    assert m["precision"] == pytest.approx(2 / 3) and m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3)
    assert np.isnan(m["p@100"])                      # fewer rows than K: undefined, not a fake number


def test_thresholds_follow_the_notebook_rule():
    """alert = best F1 on validation; ring = lowest score with precision >= 0.25, never above alert."""
    y = np.array([1, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1])
    p = np.array([0.99, 0.95, 0.9, 0.85, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1, 0.05])
    t = choose_thresholds(y, p)
    assert t["alert"] == pytest.approx(0.85)          # top 4: precision 0.75, recall 0.75, the best F1
    assert t["ring"] <= t["alert"]
    assert t["val_precision_at_ring"] >= 0.25
    assert t["ring"] == pytest.approx(0.05)           # all 12: precision 4/12, still >= 0.25


def test_split_tables_cut_at_september_11():
    ts = np.array(["2022-09-10T23:59:00", SPLIT_DATE, "2022-09-12T00:00:00", "2022-09-09T00:00:00"],
                  dtype="datetime64[ns]")
    y = np.array([1, 1, 0, 0])
    p = np.array([0.9, 0.8, 0.1, 0.2])
    tables = split_tables([("m", p, 0.5)], y, ts)
    assert tables["overall"].loc["m", "rows"] == 4
    assert tables["before"].loc["m", "rows"] == 2 and tables["after"].loc["m", "rows"] == 2
    assert tables["after"].loc["m", "tp"] == 1


def test_taint_uses_only_earlier_inbound_scores_within_24_hours():
    """A receives a 0.9 transfer and a 0.1 transfer, then pays B. The payment's sender taint
    is built from those two only; its own score never feeds its own clues."""
    t0 = 1_000_000
    ts = np.array([t0, t0 + 1, t0 + 2, t0 + 3, t0 + DAY + 2])
    src = np.array(["X", "Y", "A", "B", "A"])
    dst = np.array(["A", "A", "B", "C", "B"])
    amount = np.array([300.0, 100.0, 50.0, 10.0, 5.0])
    score = np.array([0.9, 0.1, 0.99, 0.5, 0.3])
    T = taint_clues(ts, src, dst, amount, score, progress=0)
    c = dict(zip(TAINT_NAMES, T[2]))
    assert c["snd_taint_max"] == pytest.approx(0.9)
    assert c["snd_taint_mean"] == pytest.approx(0.5)
    assert c["snd_taint_share"] == pytest.approx((300 * 0.9 + 100 * 0.1) / 400)
    assert c["rcv_taint_max"] == 0.0 and c["rcv_taint_mean"] == 0.0     # nothing reached B before
    # B's onward payment sees A's 0.99 transfer into B
    assert T[3][0] == pytest.approx(0.99)
    # a day later the 0.9 and 0.1 arrivals at A have aged out; the 0.99 into B is exactly
    # 24 h old and still counts, the same edge rule as the clue windows in common/features.py
    later = dict(zip(TAINT_NAMES, T[4]))
    assert later["snd_taint_max"] == 0.0 and later["snd_taint_share"] == 0.0
    assert later["rcv_taint_max"] == pytest.approx(0.99)
    # the first row of all has no history at all
    assert not T[0].any()
