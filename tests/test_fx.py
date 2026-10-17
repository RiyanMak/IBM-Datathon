"""Tests for common/fx.py (rates learned from the data, conversion to USD)."""

import math

import numpy as np
import pandas as pd
import pytest

from common.clean_transactions import load_raw, normalize, sort_and_assign_ids
from common.fx import (
    MissingRateError,
    apply_rates,
    fit_rates,
    fx_candidates,
    fx_consistency,
)
from common.splits import assign_split, compute_cutoffs

USD = "US Dollar"


def make_frame(rows, with_label=True):
    """rows: list of (amt_paid, ccy_paid, amt_recv, ccy_recv[, label])."""
    df = pd.DataFrame({
        "source_row": pd.Series(range(len(rows)), dtype="int64"),
        "amt_recv": [float(r[2]) for r in rows],
        "ccy_recv": pd.Series([r[3] for r in rows], dtype="category"),
        "amt_paid": [float(r[0]) for r in rows],
        "ccy_paid": pd.Series([r[1] for r in rows], dtype="category"),
        "self_transfer": False,
    })
    if with_label:
        df["label"] = pd.Series([r[4] if len(r) > 4 else 0 for r in rows], dtype="int8")
    return df


def currencies_of(df):
    return set(df["ccy_paid"].astype(object)) | set(df["ccy_recv"].astype(object))


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def test_both_directions_give_usd_per_unit():
    df = make_frame([
        (61.06, USD, 52.11, "Euro"),     # paid USD, received Euro
        (100.0, "Euro", 117.18, USD),    # paid Euro, received USD
    ])
    cand = fx_candidates(df)
    assert cand["currency"].tolist() == ["Euro", "Euro"]
    by_dir = dict(zip(cand["direction"], cand["ratio"]))
    assert by_dir["paid_usd_recv_ccy"] == pytest.approx(61.06 / 52.11)
    assert by_dir["paid_ccy_recv_usd"] == pytest.approx(1.1718)
    assert list(cand.columns) == ["currency", "ratio", "direction", "source_row"]


def test_same_currency_and_non_usd_cross_pairs_are_not_candidates():
    df = make_frame([
        (10.0, "Euro", 10.0, "Euro"),
        (10.0, USD, 10.0, USD),
        (10.0, "Euro", 1500.0, "Yen"),
        (100.0, "Euro", 117.0, USD),     # the only candidate
    ])
    cand = fx_candidates(df)
    assert cand["source_row"].tolist() == [3]


def test_zero_amounts_are_not_candidates():
    df = make_frame([
        (0.0, "Euro", 0.0, USD),
        (0.0, USD, 5.0, "Euro"),
        (5.0, USD, 0.0, "Euro"),
        (1.0, "Euro", 1.2, USD),
    ])
    assert fx_candidates(df)["source_row"].tolist() == [3]


def test_self_transfers_are_candidates():
    df = make_frame([(100.0, "Euro", 120.0, USD)])
    df["self_transfer"] = True
    assert len(fx_candidates(df)) == 1


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def test_median_odd_count():
    df = make_frame([(1.0, "Euro", r, USD) for r in (1.0, 1.3, 1.1)])
    fit = fit_rates(df, currencies_of(df), min_support=1)
    assert fit["rates"]["Euro"]["rate"] == pytest.approx(1.1)
    assert fit["rates"]["Euro"]["n_obs"] == 3


def test_median_even_count_averages_middle_values():
    df = make_frame([(1.0, "Euro", r, USD) for r in (1.0, 1.4, 1.1, 1.2)])
    fit = fit_rates(df, currencies_of(df), min_support=1)
    assert fit["rates"]["Euro"]["rate"] == pytest.approx(1.15)


def test_fit_structure_and_usd_fixed():
    df = make_frame([
        (100.0, "Euro", 120.0, USD),
        (120.0, USD, 100.0, "Euro"),
    ])
    fit = fit_rates(df, currencies_of(df), min_support=1)
    assert fit["unit"] == "USD per 1 unit of currency"
    assert fit["rates"][USD] == {"rate": 1.0, "fixed": True, "n_obs": 0}
    euro = fit["rates"]["Euro"]
    assert euro["n_paid_ccy_recv_usd"] == 1 and euro["n_paid_usd_recv_ccy"] == 1
    assert euro["min"] <= euro["p25"] <= euro["median"] <= euro["p75"] <= euro["max"]
    assert fit["total_observations"] == 2
    assert fit["warnings"] == []


def test_labels_do_not_change_rates():
    rows = [(100.0, "Euro", 110.0 + i, USD, i % 2) for i in range(5)]
    fit_a = fit_rates(make_frame(rows), {USD, "Euro"}, min_support=1)
    flipped = [r[:4] + (1 - r[4],) for r in rows]
    fit_b = fit_rates(make_frame(flipped), {USD, "Euro"}, min_support=1)
    fit_c = fit_rates(make_frame(rows, with_label=False), {USD, "Euro"}, min_support=1)
    assert fit_a == fit_b == fit_c


def test_currency_without_training_evidence_raises():
    train = make_frame([
        (100.0, "Euro", 110.0, USD),
        (5.0, "Yen", 5.0, "Yen"),        # Yen only same-currency: no evidence
    ])
    with pytest.raises(MissingRateError) as info:
        fit_rates(train, {USD, "Euro", "Yen", "Rupee"}, min_support=1)
    assert info.value.currencies == ["Rupee", "Yen"]
    assert info.value.row_counts == {"Rupee": 0, "Yen": 1}
    assert "Yen" in str(info.value) and "Rupee" in str(info.value)


def test_low_support_flag_and_warning():
    rows = [(100.0, "Euro", 110.0, USD)] * 3 + [(1.0, "Yen", 0.01, USD)] * 40
    fit = fit_rates(make_frame(rows), {USD, "Euro", "Yen"}, min_support=30)
    assert fit["rates"]["Euro"]["low_support"] is True
    assert fit["rates"]["Yen"]["low_support"] is False
    assert len(fit["warnings"]) == 1 and "Euro" in fit["warnings"][0]


def test_row_order_does_not_change_rates():
    rng = np.random.default_rng(0)
    rows = [(100.0, "Euro", float(x), USD) for x in rng.uniform(100, 130, 50)]
    rows += [(float(x), USD, 1.0, "Yen") for x in rng.uniform(0.005, 0.01, 41)]
    df = make_frame(rows)
    shuffled = df.sample(frac=1.0, random_state=1)
    assert fit_rates(df, currencies_of(df)) == fit_rates(shuffled, currencies_of(df))


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def simple_fit():
    df = make_frame([
        (100.0, "Euro", 117.18, USD),
        (1.0, "Bitcoin", 30000.0, USD),
    ])
    return fit_rates(df, currencies_of(df), min_support=1)


def test_usd_rows_unchanged_exactly():
    df = make_frame([(0.1, USD, 0.1, USD), (3697.34, USD, 3697.34, USD), (0.0, USD, 0.0, USD)])
    out = apply_rates(df, simple_fit())
    assert out.name == "amount_usd"
    assert out.dtype == "float64"
    assert out.tolist() == df["amt_paid"].tolist()


def test_bitcoin_six_decimals_and_euro():
    df = make_frame([(0.025852, "Bitcoin", 0.025852, "Bitcoin"), (50.0, "Euro", 50.0, "Euro")])
    out = apply_rates(df, simple_fit())
    assert out.iloc[0] == pytest.approx(0.025852 * 30000.0)
    assert out.iloc[1] == pytest.approx(50.0 * 1.1718)


def test_apply_keeps_index_and_order():
    df = make_frame([(2.0, "Euro", 2.0, "Euro"), (3.0, USD, 3.0, USD)])
    df.index = [10, 5]
    out = apply_rates(df, simple_fit())
    assert out.index.tolist() == [10, 5]
    assert out.loc[5] == 3.0


def test_unknown_currency_in_apply_raises():
    df = make_frame([(1.0, "Rupee", 1.0, "Rupee"), (1.0, "Rupee", 1.0, USD), (1.0, USD, 1.0, USD)])
    with pytest.raises(MissingRateError) as info:
        apply_rates(df, simple_fit())
    assert info.value.row_counts == {"Rupee": 2}


def test_non_finite_result_raises():
    fit = simple_fit()
    fit["rates"]["Euro"]["rate"] = float("inf")
    with pytest.raises(ValueError, match="not finite"):
        apply_rates(make_frame([(1.0, "Euro", 1.0, "Euro")]), fit)


def test_fx_consistency_returns_finite_numbers():
    df = make_frame([
        (100.0, "Euro", 117.18, USD),    # agrees with the fitted Euro rate
        (100.0, "Euro", 120.0, USD),     # disagrees a little
        (5.0, USD, 5.0, USD),
        (1.0, USD, 80.0, "Rupee"),       # receiving currency has no rate
    ])
    before = df.copy()
    result = fx_consistency(df, simple_fit())
    assert result["skipped_no_recv_rate"] == 1
    assert result["all_rows"]["count"] == 3
    assert result["cross_currency_rows"]["count"] == 2
    for key in ("median", "p90", "p99", "max"):
        assert math.isfinite(result["all_rows"][key])
    # amount_usd of the second row is 100 * 1.1718 = 117.18; received 120 USD.
    assert result["all_rows"]["max"] == pytest.approx((120.0 - 117.18) / 117.18)
    assert result["all_rows"]["median"] == pytest.approx(0.0)
    pd.testing.assert_frame_equal(df, before)


# ---------------------------------------------------------------------------
# Isolation: validation/test rows must not influence the rates
# ---------------------------------------------------------------------------

HEADER = (
    "Timestamp,From Bank,Account,To Bank,Account,Amount Received,"
    "Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering"
)


def fixture_rows(scale_later=1.0):
    """20 rows, one per minute; rows from minute 12 on get scaled amounts."""
    rows = []
    for i in range(20):
        factor = scale_later if i >= 12 else 1.0
        if i % 2 == 0:
            paid, ccy_p, recv, ccy_r = 100.0, "Euro", 110.0 + i, USD
        else:
            paid, ccy_p, recv, ccy_r = 0.01 * (1 + i / 100), USD, 1.0, "Yen"
        rows.append(
            f"2022/09/01 00:{i:02d},010,A{i},020,B{i},"
            f"{recv * factor:.6f},{ccy_r},{paid * factor:.6f},{ccy_p},Wire,0"
        )
    return rows


def pipeline_rates(tmp_path, rows, name):
    path = tmp_path / name
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    df = sort_and_assign_ids(normalize(load_raw(path)))
    df["split"] = assign_split(df["ts"], compute_cutoffs(df["ts"]))
    train = df[df["split"] == "train"]
    return fit_rates(train, currencies_of(df), min_support=1), df


def test_rates_ignore_validation_and_test_rows(tmp_path):
    fit_a, df_a = pipeline_rates(tmp_path, fixture_rows(1.0), "a.csv")
    fit_b, df_b = pipeline_rates(tmp_path, fixture_rows(1000.0), "b.csv")
    # Sanity: the scaling really only touched rows outside train.
    assert (df_a["split"] == "train").sum() == 12
    assert df_a.loc[df_a["split"] != "train", "amt_paid"].iloc[0] * 1000 == pytest.approx(
        df_b.loc[df_b["split"] != "train", "amt_paid"].iloc[0]
    )
    assert fit_a == fit_b
