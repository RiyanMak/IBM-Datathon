"""Tests for common/features.py, the clue class shared by the notebook and the live scorer."""

import math

import numpy as np
import pytest

from common.features import DAY, FEATURE_NAMES, FORMATS, ClueBuilder, to_minutes

T0 = to_minutes("2022-09-08T14:00:00")


def txn(ts_min, src, dst, amount, fmt="ACH", ccy_paid="US Dollar", ccy_recv="US Dollar"):
    """One transfer as the scorer receives it from the txns stream: every field is text."""
    return {
        "ts": f"2022-09-08T{ts_min // 60 % 24:02d}:{ts_min % 60:02d}:00",
        "from_acct": src, "to_acct": dst, "amount_usd": str(amount),
        "amt_paid": str(amount), "ccy_paid": ccy_paid,
        "amt_recv": str(amount), "ccy_recv": ccy_recv, "fmt": fmt,
    }


def clues(builder, ts_min, src, dst, amount, **kw):
    """Run one transfer through the offline (list) path and return {name: value}."""
    row = builder.compute_row(ts_min, src, dst, amount, kw.get("ccy_paid", "US Dollar"),
                              kw.get("ccy_recv", "US Dollar"), kw.get("fmt", "ACH"))
    return dict(zip(FEATURE_NAMES, row))


def test_feature_names_are_unique_and_match_row_length():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    row = ClueBuilder().compute_row(T0, "10_A", "10_B", 100.0, "US Dollar", "US Dollar", "ACH")
    assert len(row) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in row)
    assert not any(math.isnan(v) for v in row)


def test_simple_clues_from_the_transfer_itself():
    c = clues(ClueBuilder(), T0, "10_A", "27_B", 1600.0, fmt="Wire", ccy_paid="Euro")
    assert c["log_amount"] == pytest.approx(math.log10(1601))
    assert c["is_round"] == 1.0
    assert c["fmt_Wire"] == 1.0
    assert sum(c[f"fmt_{f.replace(' ', '_')}"] for f in FORMATS) == 1.0
    assert c["cross_currency"] == 1.0
    assert c["cross_bank"] == 1.0          # bank 10 to bank 27
    assert c["self_transfer"] == 0.0
    assert c["hour"] == 14.0
    assert c["weekday"] == 3.0             # 2022-09-08 was a Thursday

    c = clues(ClueBuilder(), T0, "10_A", "10_A", 1234.56)
    assert c["is_round"] == 0.0
    assert c["cross_bank"] == 0.0
    assert c["self_transfer"] == 1.0


def test_first_sight_of_both_accounts_has_no_history():
    c = clues(ClueBuilder(), T0, "10_A", "10_B", 100.0)
    assert c["snd_in_sum_24h"] == 0.0
    assert c["pass_through"] == 0.0
    assert c["mins_since_snd_last_in"] == -1.0
    assert c["mins_since_rcv_last_out"] == -1.0
    assert c["new_payee"] == 1.0
    assert c["snd_age_days"] == 0.0
    assert c["rcv_age_days"] == 0.0
    assert c["amt_vs_snd_avg"] == 0.0
    assert c["amt_vs_rcv_avg"] == 0.0


def test_scatter_gather_story_from_the_guide():
    """Student M receives 10,000, then pays six strangers 1,600 each; they all pay collector B."""
    b = ClueBuilder()
    clues(b, T0, "99_SENDER", "10_M", 10000.0)
    payees = [f"27_P{i}" for i in range(6)]
    for i, p in enumerate(payees):
        t = T0 + 25 + i * 15
        c = clues(b, t, "10_M", p, 1600.0)
        assert c["snd_in_sum_24h"] == 10000.0
        assert c["snd_out_sum_24h"] == 1600.0 * i          # the current payment is not yet counted
        assert c["pass_through"] == pytest.approx(0.16 * i)
        assert c["mins_since_snd_last_in"] == 25 + i * 15
        assert c["new_payee"] == 1.0
        assert c["snd_payees_24h"] == float(i)
        # payments 15 min apart; one exactly 60 min ago is outside the window, so at most 3 count
        assert c["snd_out_count_1h"] == float(min(i, 3))
        clues(b, t + 5, p, "27_B", 1590.0)

    # collector B now has six distinct payers in its window; M paying P0 again is not a new payee
    c = clues(b, T0 + 200, "27_P0", "27_B", 10.0)
    assert c["rcv_payers_24h"] == 6.0
    assert c["rcv_in_count_24h"] == 6.0
    assert c["rcv_in_sum_24h"] == pytest.approx(6 * 1590.0)
    c = clues(b, T0 + 201, "10_M", "27_P0", 5.0)
    assert c["new_payee"] == 0.0
    assert c["amt_vs_snd_avg"] == pytest.approx(5.0 / 1600.0)


def test_window_drops_transfers_older_than_24h():
    b = ClueBuilder()
    clues(b, T0, "10_X", "10_A", 500.0)                    # A receives 500 at T0
    c = clues(b, T0 + DAY - 1, "10_A", "10_B", 1.0)        # just inside the window
    assert c["snd_in_sum_24h"] == 500.0
    assert c["snd_in_count_24h"] == 1.0
    c = clues(b, T0 + DAY + 1, "10_A", "10_B", 1.0)        # just outside: pruned
    assert c["snd_in_sum_24h"] == 0.0
    assert c["snd_in_count_24h"] == 0.0
    # lifetime facts survive the window
    assert c["mins_since_snd_last_in"] == DAY + 1
    assert c["snd_age_days"] == pytest.approx((DAY + 1) / DAY)
    assert c["new_payee"] == 0.0                           # B was paid a minute ago


def test_window_sums_are_exact_after_pruning():
    """Float running sums left residues like -2.8e-17 once a window emptied, and a residue
    divided by a residue produced pass-through values such as -25. Sums are kept in
    integer cents now, so an emptied window is exactly zero and ratios are never negative."""
    b = ClueBuilder()
    clues(b, T0, "1_A", "1_B", 0.1)
    clues(b, T0 + 1, "1_A", "1_B", 0.2)
    clues(b, T0 + 2, "1_B", "1_C", 0.7)
    clues(b, T0 + 3, "1_B", "1_C", 0.1)
    c = clues(b, T0 + DAY + 10, "1_X", "1_B", 5.0)      # B's whole window has aged out
    assert c["rcv_in_sum_24h"] == 0.0 and c["rcv_out_sum_24h"] == 0.0
    assert c["rcv_pass_through"] == 0.0
    acct = b._accts["1_B"]                              # after remembering the 5.00 receipt
    assert acct.out_cents == 0 and len(acct.outbound) == 0
    assert acct.in_cents == 500 and len(acct.inbound) == 1

    # over many random amounts in and out, no sum or ratio ever goes negative
    rng = np.random.default_rng(1)
    b = ClueBuilder()
    for i in range(3000):
        t = T0 + i * 7
        amt = round(float(rng.lognormal(3, 2)), 2)
        src, dst = ("1_P", "1_Q") if i % 2 else ("1_Q", "1_P")
        c = clues(b, t, src, dst, amt)
        for name in ("snd_in_sum_24h", "snd_out_sum_24h", "pass_through",
                     "rcv_in_sum_24h", "rcv_out_sum_24h", "rcv_pass_through"):
            assert c[name] >= 0.0, (name, c[name])
        assert c["pass_through"] <= 5.0 and c["rcv_pass_through"] <= 5.0


def test_zero_amount_history_does_not_divide_by_zero():
    b = ClueBuilder()
    clues(b, T0, "10_A", "10_B", 0.0)      # A's only payment and B's only receipt are 0
    c = clues(b, T0 + 1, "10_A", "10_B", 50.0)
    assert c["amt_vs_snd_avg"] == 0.0
    assert c["amt_vs_rcv_avg"] == 0.0
    assert c["pass_through"] == 0.0
    assert not any(math.isnan(v) or math.isinf(v) for v in c.values())


def test_pass_through_is_capped():
    b = ClueBuilder()
    clues(b, T0, "10_X", "10_A", 10.0)         # A receives 10
    clues(b, T0 + 1, "10_A", "10_B", 1000.0)   # A sends 1000
    c = clues(b, T0 + 2, "10_A", "10_C", 1.0)
    assert c["pass_through"] == 5.0


def test_live_dict_path_matches_offline_list_path():
    """The scorer calls compute() with text fields; the notebook calls compute_row(). Same numbers."""
    rng = np.random.default_rng(0)
    accts = [f"{b}_{a:03X}" for b in (10, 27) for a in range(8)]
    transfers = []
    t = T0
    for _ in range(300):
        t += int(rng.integers(0, 3))
        src, dst = rng.choice(accts, 2)
        amt = round(float(rng.lognormal(5, 1)), 2)
        transfers.append((t, src, dst, amt, rng.choice(FORMATS), rng.choice(["US Dollar", "Euro"])))

    offline, live = ClueBuilder(), ClueBuilder()
    for t, src, dst, amt, fmt, ccy in transfers:
        a = offline.compute_row(t, src, dst, amt, ccy, "US Dollar", fmt)
        d = live.compute(txn(t, src, dst, amt, fmt=fmt, ccy_paid=ccy))
        assert list(d) == FEATURE_NAMES
        assert np.array_equal(np.array(a, dtype=np.float32), np.array(list(d.values()), dtype=np.float32))


def test_to_minutes_accepts_text_pandas_timestamps_and_datetimes():
    from datetime import datetime
    import pandas as pd
    assert to_minutes("2022-09-01T00:20:00") == to_minutes(pd.Timestamp("2022-09-01 00:20"))
    assert to_minutes("2022-09-01T00:20:00") == to_minutes(datetime(2022, 9, 1, 0, 20))
    assert to_minutes("2022-09-01T00:21:00") - to_minutes("2022-09-01T00:20:00") == 1
    assert to_minutes("2022-09-02T00:20:00") - to_minutes("2022-09-01T00:20:00") == DAY


def test_memory_is_bounded_for_hub_accounts():
    from common.features import MAX_DEQUE
    b = ClueBuilder()
    for i in range(MAX_DEQUE + 500):
        clues(b, T0 + i % 10, f"10_{i}", "10_HUB", 1.0)   # thousands of payers within minutes
    hub = b._accts["10_HUB"]
    assert len(hub.inbound) == MAX_DEQUE
    assert hub.in_sum == pytest.approx(float(MAX_DEQUE))
    assert hub.n_in == MAX_DEQUE + 500                       # lifetime count still exact


# ---------------------------------------------------------------- the seven clues added after the first 32

ORIGINAL_32 = [
    "log_amount", "is_round", "fmt_ACH", "fmt_Bitcoin", "fmt_Cash", "fmt_Cheque", "fmt_Credit_Card",
    "fmt_Reinvestment", "fmt_Wire", "cross_currency", "cross_bank", "self_transfer", "hour", "weekday",
    "snd_in_sum_24h", "snd_out_sum_24h", "pass_through", "mins_since_snd_last_in", "new_payee",
    "snd_payees_24h", "snd_in_count_24h", "snd_out_count_1h", "amt_vs_snd_avg", "snd_age_days",
    "rcv_in_sum_24h", "rcv_out_sum_24h", "rcv_pass_through", "mins_since_rcv_last_out",
    "rcv_payers_24h", "rcv_in_count_24h", "amt_vs_rcv_avg", "rcv_age_days",
]


def test_first_32_names_keep_their_order():
    """Models trained before the new clues read these 32 by name; new clues only go at the end."""
    assert FEATURE_NAMES[:32] == ORIGINAL_32
    assert FEATURE_NAMES[32:] == ["back_and_forth_24h", "snd_payees_6h", "rcv_payers_6h",
                                  "snd_out_per_payee_24h", "snd_amt_std_ratio", "rcv_amt_std_ratio",
                                  "snd_gap_mean_min"]


def test_new_clues_with_no_history():
    """Counts and ratios are 0 with no history; the mean gap is -1 like the other 'minutes since' clues."""
    c = clues(ClueBuilder(), T0, "10_A", "10_B", 100.0)
    for name in ("back_and_forth_24h", "snd_payees_6h", "rcv_payers_6h", "snd_out_per_payee_24h",
                 "snd_amt_std_ratio", "rcv_amt_std_ratio"):
        assert c[name] == 0.0, name
    assert c["snd_gap_mean_min"] == -1.0


def test_back_and_forth_counts_the_receiver_paying_the_sender_in_24h():
    """B pays A twice, then A pays B: two earlier transfers the other way. One older than 24 h is gone."""
    b = ClueBuilder()
    clues(b, T0, "10_B", "10_A", 300.0)
    clues(b, T0 + DAY - 10, "10_B", "10_A", 200.0)
    clues(b, T0 + DAY - 5, "10_C", "10_A", 900.0)            # someone else paying A does not count
    c = clues(b, T0 + DAY - 1, "10_A", "10_B", 450.0)
    assert c["back_and_forth_24h"] == 2.0
    c = clues(b, T0 + DAY + 1, "10_A", "10_B", 450.0)        # the T0 payment has aged out
    assert c["back_and_forth_24h"] == 1.0
    c = clues(b, T0 + DAY + 2, "10_A", "10_D", 1.0)          # D never paid A
    assert c["back_and_forth_24h"] == 0.0


def test_six_hour_payees_and_payers_use_a_tighter_window():
    """M pays P0..P3 seven hours ago and P4, P5 an hour ago: 6 payees in 24 h, 2 in 6 h."""
    b = ClueBuilder()
    now = T0 + 8 * 60
    for i in range(4):
        clues(b, now - 7 * 60 + i, "10_M", f"27_P{i}", 100.0)
    for i in (4, 5):
        clues(b, now - 60 + i, "10_M", f"27_P{i}", 100.0)
    c = clues(b, now, "10_M", "27_X", 100.0)
    assert c["snd_payees_24h"] == 6.0
    assert c["snd_payees_6h"] == 2.0

    # the collector side: six payers into B, four of them more than 6 hours ago
    b = ClueBuilder()
    for i in range(4):
        clues(b, now - 7 * 60 + i, f"27_P{i}", "10_B", 100.0)
    clues(b, now - 30, "27_P4", "10_B", 100.0)
    clues(b, now - 20, "27_P4", "10_B", 100.0)               # the same payer twice counts once
    clues(b, now - 10, "27_P5", "10_B", 100.0)
    c = clues(b, now, "27_Q", "10_B", 100.0)
    assert c["rcv_payers_24h"] == 6.0
    assert c["rcv_payers_6h"] == 2.0


def test_out_per_payee_is_payments_divided_by_distinct_payees():
    """Six payments in 24 h to two accounts: 3 per payee. Scatter to six strangers: 1 per payee."""
    b = ClueBuilder()
    for i in range(6):
        clues(b, T0 + i, "10_A", "10_B" if i % 2 else "10_C", 50.0)
    c = clues(b, T0 + 10, "10_A", "10_D", 50.0)
    assert c["snd_out_per_payee_24h"] == pytest.approx(3.0)

    b = ClueBuilder()
    for i in range(6):
        clues(b, T0 + i, "10_M", f"27_P{i}", 1600.0)
    assert clues(b, T0 + 10, "10_M", "27_X", 1.0)["snd_out_per_payee_24h"] == pytest.approx(1.0)


def test_amount_spread_is_std_over_mean_of_earlier_amounts():
    """Population standard deviation over mean of the sender's earlier payments (and the
    receiver's earlier receipts), lifetime, not windowed."""
    amounts = [100.0, 300.0, 200.0, 1000.0]
    b = ClueBuilder()
    for i, amt in enumerate(amounts):
        clues(b, T0 + i * DAY, "10_A", f"10_R{i}", amt)       # spread over days: lifetime, not 24 h
    c = clues(b, T0 + 10 * DAY, "10_A", "10_Z", 5.0)
    assert c["snd_amt_std_ratio"] == pytest.approx(np.std(amounts) / np.mean(amounts))

    b = ClueBuilder()
    for i, amt in enumerate(amounts):
        clues(b, T0 + i * DAY, f"10_S{i}", "10_B", amt)
    c = clues(b, T0 + 10 * DAY, "10_Z", "10_B", 5.0)
    assert c["rcv_amt_std_ratio"] == pytest.approx(np.std(amounts) / np.mean(amounts))
    assert c["snd_amt_std_ratio"] == 0.0                       # Z has never paid anyone

    # identical amounts have no spread, and one earlier amount has none either
    b = ClueBuilder()
    for i in range(5):
        clues(b, T0 + i, "10_A", "10_B", 1600.0)
    c = clues(b, T0 + 9, "10_A", "10_B", 1600.0)
    assert c["snd_amt_std_ratio"] == 0.0 and c["rcv_amt_std_ratio"] == 0.0


def test_amount_spread_is_exact_and_never_negative_for_awkward_amounts():
    """Sums are kept in integer cents, so many near-identical large amounts cannot leave a
    negative variance behind (a float sum of squares would)."""
    b = ClueBuilder()
    for i in range(500):
        clues(b, T0 + i, "10_A", "10_B", 123456789.01 + (i % 2) * 0.01)
    c = clues(b, T0 + 600, "10_A", "10_B", 1.0)
    assert 0.0 <= c["snd_amt_std_ratio"] < 1e-9
    assert not math.isnan(c["snd_amt_std_ratio"])


def test_mean_gap_between_the_senders_payments():
    """Payments at 0, 10 and 40 minutes: gaps 10 and 30, mean 20. Lifetime, so old gaps count."""
    b = ClueBuilder()
    c = clues(b, T0, "10_A", "10_B", 10.0)
    assert c["snd_gap_mean_min"] == -1.0                       # never paid before
    c = clues(b, T0 + 10, "10_A", "10_C", 10.0)
    assert c["snd_gap_mean_min"] == -1.0                       # one earlier payment: no gap yet
    c = clues(b, T0 + 40, "10_A", "10_D", 10.0)
    assert c["snd_gap_mean_min"] == pytest.approx(10.0)
    c = clues(b, T0 + 2 * DAY, "10_A", "10_E", 10.0)
    assert c["snd_gap_mean_min"] == pytest.approx(20.0)
    clues(b, T0 + 2 * DAY + 1, "10_X", "10_A", 10.0)           # receiving money is not a payment
    c = clues(b, T0 + 2 * DAY + 2, "10_A", "10_E", 10.0)
    assert c["snd_gap_mean_min"] == pytest.approx((2 * DAY) / 3)
