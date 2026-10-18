"""Tests for scripts/stress_replayer.py, on fakeredis."""
import json

import pandas as pd
import pytest

from common import bus
from common.clean_transactions import write_clean_csv
from scripts.stress_replayer import main, run


def rows(n):
    return pd.DataFrame({
        "txn_id": pd.Series(range(n), dtype="int64"),
        "ts": pd.to_datetime(["2022-09-08T16:%02d:00" % (12 + i // 10) for i in range(n)], format="ISO8601"),
        "from_acct": ["10_A%03d" % i for i in range(n)],
        "to_acct": ["20_B%03d" % i for i in range(n)],
        "amount_usd": [10.0 + i for i in range(n)],
        "amt_paid": [10.0 + i for i in range(n)],
        "ccy_paid": pd.Categorical(["US Dollar"] * n),
        "amt_recv": [10.0 + i for i in range(n)],
        "ccy_recv": pd.Categorical(["US Dollar"] * n),
        "fmt": pd.Categorical(["ACH"] * n),
    })


def test_run_reports_rates_and_cleans_up():
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.FakeRedis(decode_responses=True)
    ticks = iter(range(0, 1000, 2))     # every clock reading is two seconds after the last
    result = run(r, rows(50), repeat=2, stream="stress-test", maxlen=1000, clock_fn=lambda: float(next(ticks)))
    assert result["runs"] == 2 and result["messages"] == 50 and len(result["rates"]) == 2
    assert result["median_msg_per_s"] > 0
    assert r.exists("stress-test") == 0            # the scratch stream is gone
    assert r.xlen(bus.TXNS) == 0                   # the real stream was never touched


def test_main_reads_the_files_and_refuses_real_streams(tmp_path):
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.FakeRedis(decode_responses=True)
    df = rows(30)
    df["label"] = 0
    df["split"] = "test"
    clean, splits, out = tmp_path / "clean.csv", tmp_path / "splits.json", tmp_path / "stress.json"
    write_clean_csv(df, clean)
    splits.write_text(json.dumps({"test_start": "2022-09-08T16:13:00", "provenance": {"mode": "full"}}))
    result = main(["--clean", str(clean), "--splits", str(splits), "--limit", "15", "--repeat", "1",
                   "--out", str(out)], r=r)
    assert result["messages"] == 15 and json.loads(out.read_text())["runs"] == 1
    with pytest.raises(SystemExit):
        main(["--clean", str(clean), "--splits", str(splits), "--stream", bus.TXNS], r=r)
