"""Tests for scripts/stress_replayer.py. fakeredis stands in for Redis (skipped if missing)."""
import pandas as pd
import pytest

from common import bus
from scripts import stress_replayer


def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(decode_responses=True)


def make_rows(n):
    """A small frame shaped like clean.csv's transaction columns (as in the replayer tests)."""
    return pd.DataFrame({
        "txn_id": pd.Series(range(n), dtype="int64"),
        "ts": pd.to_datetime(["2022-09-08T16:%02d:00" % (12 + i // 50) for i in range(n)], format="ISO8601"),
        "from_acct": ["10_A%03d" % i for i in range(n)],
        "to_acct": ["20_B%03d" % i for i in range(n)],
        "amount_usd": [10.5 + i for i in range(n)],
        "amt_paid": [10.5 + i for i in range(n)],
        "ccy_paid": pd.Categorical(["US Dollar"] * n),
        "amt_recv": [10.5 + i for i in range(n)],
        "ccy_recv": pd.Categorical(["US Dollar"] * n),
        "fmt": pd.Categorical(["ACH"] * n),
    })


def test_stress_sends_limit_prints_rate_and_deletes_the_stream(capsys):
    r = fake_redis()
    sent, seconds, rate = stress_replayer.main(["--limit", "120"], r=r, rows=make_rows(300))
    assert sent == 120 and seconds >= 0 and rate > 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("120 messages in ") and out.endswith(" msg/s") and " s = " in out
    assert not r.exists("txns_stress")


def test_keep_leaves_the_capped_stream():
    r = fake_redis()
    r.xadd("txns_stress", {"old": "1"})            # a leftover is cleared first
    stress_replayer.main(["--limit", "200", "--maxlen", "50", "--keep"], r=r, rows=make_rows(200))
    assert r.exists("txns_stress")
    assert r.xlen("txns_stress") <= 200
    assert "old" not in r.xrange("txns_stress", count=1)[0][1]


def test_never_touches_the_live_stream():
    r = fake_redis()
    with pytest.raises(ValueError):
        stress_replayer.stress(r, make_rows(5), stream=bus.TXNS)
    with pytest.raises(SystemExit):
        stress_replayer.main(["--stream", bus.TXNS], r=r, rows=make_rows(5))


def test_result_line_format():
    assert stress_replayer.result_line(100000, 3.456, 28935.2) == "100000 messages in 3.46 s = 28935 msg/s"
