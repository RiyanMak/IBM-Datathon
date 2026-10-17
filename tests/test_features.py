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


def test_to_minutes_accepts_text_and_pandas_timestamps():
    import pandas as pd
    assert to_minutes("2022-09-01T00:20:00") == to_minutes(pd.Timestamp("2022-09-01 00:20"))
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
