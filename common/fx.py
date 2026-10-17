"""Exchange rates learned from the data, and conversion of amounts to USD.

Every rate means "USD per 1 unit of currency C". US Dollar is fixed at 1.0.

The rates are learned only from rows where exactly one side of the payment is
in US Dollar (for example: paid in Euro, received in US Dollar). Each such row
gives one observed ratio; the rate of a currency is the median of its ratios
in the TRAINING rows. Labels are never used.

Typical use:

    fit = fit_rates(train_rows, all_currencies)
    amount_usd = apply_rates(all_rows, fit)
"""

import numpy as np
import pandas as pd

from common.data_contract import USD

UNIT_TEXT = "USD per 1 unit of currency"

METHOD_TEXT = (
    "For each non-USD currency C, take every training row where one side is C "
    "and the other side is US Dollar, with both amounts above zero. "
    "If C was paid and USD received, the ratio is amt_recv / amt_paid; "
    "if USD was paid and C received, the ratio is amt_paid / amt_recv. "
    "The rate is the median of these ratios. Self-transfers are included. "
    "Labels are not used. US Dollar is fixed at 1.0."
)

DIRECTION_PAID_CCY = "paid_ccy_recv_usd"
DIRECTION_PAID_USD = "paid_usd_recv_ccy"


class MissingRateError(Exception):
    """Raised when a currency needs a rate but none can be learned.

    `currencies` is a sorted list of the currency names and `row_counts` a
    dict {currency: number of affected rows}.
    """

    def __init__(self, currencies, row_counts=None, context=""):
        self.currencies = sorted(currencies)
        self.row_counts = dict(row_counts or {})
        parts = []
        for c in self.currencies:
            if c in self.row_counts:
                parts.append(f"{c} ({self.row_counts[c]} row(s))")
            else:
                parts.append(c)
        message = "no exchange rate for: " + ", ".join(parts)
        if context:
            message += f" [{context}]"
        super().__init__(message)


def _text(s: pd.Series) -> pd.Series:
    """Currency column as plain text, so two categoricals can be compared."""
    return s.astype(object)


def fx_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """One row per observed USD exchange ratio.

    Columns: currency, ratio, direction, source_row.
    Only ccy_paid, ccy_recv, amt_paid, amt_recv and source_row are read.
    """
    paid = _text(df["ccy_paid"])
    recv = _text(df["ccy_recv"])
    amt_paid = df["amt_paid"].to_numpy(dtype="float64")
    amt_recv = df["amt_recv"].to_numpy(dtype="float64")

    amounts_ok = (
        np.isfinite(amt_paid) & np.isfinite(amt_recv) & (amt_paid > 0) & (amt_recv > 0)
    )
    paid_ccy_recv_usd = (paid != USD).to_numpy() & (recv == USD).to_numpy() & amounts_ok
    paid_usd_recv_ccy = (paid == USD).to_numpy() & (recv != USD).to_numpy() & amounts_ok

    source_row = df["source_row"].to_numpy(dtype="int64")
    part1 = pd.DataFrame({
        "currency": paid.to_numpy()[paid_ccy_recv_usd],
        "ratio": amt_recv[paid_ccy_recv_usd] / amt_paid[paid_ccy_recv_usd],
        "direction": DIRECTION_PAID_CCY,
        "source_row": source_row[paid_ccy_recv_usd],
    })
    part2 = pd.DataFrame({
        "currency": recv.to_numpy()[paid_usd_recv_ccy],
        "ratio": amt_paid[paid_usd_recv_ccy] / amt_recv[paid_usd_recv_ccy],
        "direction": DIRECTION_PAID_USD,
        "source_row": source_row[paid_usd_recv_ccy],
    })
    out = pd.concat([part1, part2], ignore_index=True)
    out["currency"] = out["currency"].astype(object)
    out["ratio"] = out["ratio"].astype("float64")
    out["direction"] = out["direction"].astype(object)
    out["source_row"] = out["source_row"].astype("int64")
    return out


def _currency_row_counts(df: pd.DataFrame, currencies) -> dict:
    """Number of rows of df that use each currency on either side."""
    paid = _text(df["ccy_paid"])
    recv = _text(df["ccy_recv"])
    return {c: int(((paid == c) | (recv == c)).sum()) for c in currencies}


def fit_rates(train_df: pd.DataFrame, all_currencies, min_support=30) -> dict:
    """Learn one rate per currency from the TRAINING rows only.

    all_currencies: every currency that appears anywhere in the full data
    (both currency columns, all splits). Every one of them must get a rate.
    Raises MissingRateError if a currency has no training evidence.
    """
    all_currencies = sorted(set(all_currencies))
    train_currencies = set(_text(train_df["ccy_paid"])) | set(_text(train_df["ccy_recv"]))
    unknown = train_currencies - set(all_currencies)
    if unknown:
        raise ValueError(
            f"training rows use currencies missing from all_currencies: {sorted(unknown)}"
        )

    candidates = fx_candidates(train_df)
    non_usd = [c for c in all_currencies if c != USD]

    # Stop before fitting anything if some currency has no evidence at all.
    with_evidence = set(candidates["currency"])
    missing = [c for c in non_usd if c not in with_evidence]
    if missing:
        raise MissingRateError(
            missing,
            _currency_row_counts(train_df, missing),
            context="row counts are training rows using the currency",
        )

    rates = {USD: {"rate": 1.0, "fixed": True, "n_obs": 0}}
    warnings = []
    for currency in non_usd:
        group = candidates[candidates["currency"] == currency]
        # Sorting first makes the result independent of the input row order.
        ratios = np.sort(group["ratio"].to_numpy())
        n_obs = len(ratios)
        q = np.quantile(ratios, [0.01, 0.25, 0.75, 0.99])
        low_support = n_obs < min_support
        rates[currency] = {
            "rate": float(np.median(ratios)),
            "n_obs": int(n_obs),
            "n_paid_ccy_recv_usd": int((group["direction"] == DIRECTION_PAID_CCY).sum()),
            "n_paid_usd_recv_ccy": int((group["direction"] == DIRECTION_PAID_USD).sum()),
            "min": float(ratios[0]),
            "p01": float(q[0]),
            "p25": float(q[1]),
            "median": float(np.median(ratios)),
            "p75": float(q[2]),
            "p99": float(q[3]),
            "max": float(ratios[-1]),
            "low_support": bool(low_support),
        }
        if low_support:
            warnings.append(
                f"{currency}: only {n_obs} training observation(s) "
                f"(fewer than {min_support}); rate may be unreliable"
            )

    return {
        "unit": UNIT_TEXT,
        "method": METHOD_TEXT,
        "rates": rates,
        "total_observations": int(len(candidates)),
        "warnings": warnings,
    }


def _rate_series(ccy: pd.Series, fit: dict) -> pd.Series:
    """Rate for every row's currency (NaN where there is no rate)."""
    rate_map = {c: info["rate"] for c, info in fit["rates"].items()}
    return _text(ccy).map(rate_map).astype("float64")


def apply_rates(df: pd.DataFrame, fit: dict) -> pd.Series:
    """amount_usd = amt_paid * rate[ccy_paid], aligned with df's index.

    Raises MissingRateError if a payment currency has no rate and
    ValueError if any result is not a finite number.
    """
    rate = _rate_series(df["ccy_paid"], fit)
    if rate.isna().any():
        missing = _text(df.loc[rate.isna(), "ccy_paid"]).value_counts()
        raise MissingRateError(
            missing.index.tolist(),
            {c: int(n) for c, n in missing.items()},
            context="row counts are rows paid in the currency",
        )

    amount_usd = (df["amt_paid"].astype("float64") * rate).rename("amount_usd")
    not_finite = ~np.isfinite(amount_usd.to_numpy())
    if not_finite.any():
        raise ValueError(f"{int(not_finite.sum())} amount_usd value(s) are not finite")
    return amount_usd


def _describe(values: np.ndarray) -> dict:
    """count / median / p90 / p99 / max of an array (None when empty)."""
    if len(values) == 0:
        return {"count": 0, "median": None, "p90": None, "p99": None, "max": None}
    return {
        "count": int(len(values)),
        "median": float(np.median(values)),
        "p90": float(np.quantile(values, 0.90)),
        "p99": float(np.quantile(values, 0.99)),
        "max": float(np.max(values)),
    }


def fx_consistency(df: pd.DataFrame, fit: dict) -> dict:
    """Diagnostic only: how well the paid side and received side agree in USD.

    For each row with amount_usd > 0, the relative difference is
    |amt_recv * rate[ccy_recv] - amount_usd| / amount_usd.
    Rows whose receiving currency has no rate are skipped and counted.
    Nothing in df is changed.
    """
    amount_usd = apply_rates(df, fit).to_numpy()
    recv_rate = _rate_series(df["ccy_recv"], fit).to_numpy()
    recv_usd = df["amt_recv"].to_numpy(dtype="float64") * recv_rate

    no_recv_rate = np.isnan(recv_rate)
    usable = (amount_usd > 0) & ~no_recv_rate
    rel_diff = np.abs(recv_usd[usable] - amount_usd[usable]) / amount_usd[usable]

    cross = (_text(df["ccy_paid"]) != _text(df["ccy_recv"])).to_numpy()[usable]
    return {
        "all_rows": _describe(rel_diff),
        "cross_currency_rows": _describe(rel_diff[cross]),
        "skipped_no_recv_rate": int(no_recv_rate.sum()),
        "skipped_zero_amount_usd": int(((amount_usd <= 0) & ~no_recv_rate).sum()),
    }
