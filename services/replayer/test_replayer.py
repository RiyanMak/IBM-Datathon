"""
Tests for the replayer and the txns message format (common/streams.py).
Run from the repo root:
    python -m pytest -q services/replayer
No Redis needed: the Redis tests use fakeredis (pip install -r requirements-dev.txt)
and are skipped if it is not installed.
"""
import json
import threading
from datetime import datetime

import pandas as pd
import pytest

from common import bus
from common.clean_transactions import read_clean_csv, write_clean_csv
from common.features import ClueBuilder
from common.streams import (
    TXN_FIELDS,
    TXN_COLUMNS,
    format_epoch,
    message_to_txn,
    now_sent_at,
    txn_to_message,
)
from services.replayer import replayer
from services.replayer.replayer import (
    choose_start,
    load_rows,
    main,
    replay,
)

TEST_START = "2022-09-08T16:12:00"


# ---------------------------------------------------------------- helpers

class FakeClock:
    """A clock that only moves when someone 'sleeps'. Records every sleep.

    cost > 0 makes every clock reading also move time forward a little, like
    real work taking time.
    """
    def __init__(self, cost=0.0):
        self.now = 1000.0
        self.cost = cost
        self.sleeps = []

    def clock(self):
        self.now += self.cost
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(decode_responses=True)


def make_rows(timestamps, first_id=0):
    """A small frame shaped like clean.csv's transaction columns."""
    n = len(timestamps)
    return pd.DataFrame({
        "txn_id": pd.Series(range(first_id, first_id + n), dtype="int64"),
        "ts": pd.to_datetime(timestamps, format="ISO8601"),
        "from_acct": ["10_A%03d" % i for i in range(n)],
        "to_acct": ["20_B%03d" % i for i in range(n)],
        "amount_usd": [0.025852 + i for i in range(n)],
        "amt_paid": [4197895233.69] * n,
        "ccy_paid": pd.Categorical(["US Dollar"] * n),
        "amt_recv": [0.1 + 0.2] * n,
        "ccy_recv": pd.Categorical(["Euro"] * n),
        "fmt": pd.Categorical(["Cheque"] * n),
    })


def minutes(start, count, step=1):
    t0 = pd.Timestamp(start)
    return [t0 + pd.Timedelta(minutes=i * step) for i in range(count)]


@pytest.fixture
def fixture_files(tmp_path):
    """A tiny clean.csv (2 train rows, 6 test rows) and splits.json.

    The label column holds text that is not a valid label, and split says
    'train' for every row: if the replayer ever used them, tests would notice.
    """
    ts = ["2022-09-08T16:10:00", "2022-09-08T16:11:00",            # before the test start
          "2022-09-08T16:12:00", "2022-09-08T16:12:00",             # tied timestamp
          "2022-09-08T16:13:00", "2022-09-08T16:20:00",
          "2022-09-08T16:30:00", "2022-09-08T17:00:00"]
    df = make_rows(ts)
    df["source_row"] = range(100, 100 + len(df))
    df["label"] = ["NOT-A-LABEL"] * len(df)
    df["split"] = ["train"] * len(df)
    # Store the rows shuffled, so the replayer has to sort them itself.
    clean = tmp_path / "clean.csv"
    write_clean_csv(df.sample(frac=1, random_state=1), clean)

    splits = tmp_path / "splits.json"
    splits.write_text(json.dumps({
        "validation_start": "2022-09-08T16:00:00",
        "test_start": TEST_START,
        "provenance": {"mode": "full"},
    }))
    return {"clean": str(clean), "splits": str(splits), "df": df}


def cli(files, *extra):
    return ["--clean", files["clean"], "--splits", files["splits"], *extra]


# ---------------------------------------------------------------- message format

def test_message_has_exactly_the_contract_fields():
    row = make_rows(["2022-09-08T16:12:00"]).iloc[0].to_dict()
    row["label"] = 1            # extra keys must be ignored, never copied
    row["split"] = "test"
    row["source_row"] = 7
    msg = txn_to_message(row, now_sent_at())
    assert tuple(msg) == TXN_FIELDS
    assert all(isinstance(v, str) for v in msg.values())
    for leak in ("label", "split", "source_row"):
        assert leak not in msg


def test_round_trip_is_exact():
    values = [0.025852, 4197895233.69, 0.1 + 0.2, 1e-9, 123456789012.34567]
    for value in values:
        row = make_rows(["2022-09-08T16:12:00"]).iloc[0].to_dict()   # numpy / pandas types
        row["amount_usd"] = value
        row["amt_paid"] = value
        row["amt_recv"] = value
        txn = message_to_txn(txn_to_message(row, "1792345678.123"))
        assert txn["amount_usd"] == value and txn["amt_paid"] == value and txn["amt_recv"] == value
    assert txn["txn_id"] == 0 and isinstance(txn["txn_id"], int)
    assert txn["ts"] == datetime(2022, 9, 8, 16, 12, 0) and txn["ts"].tzinfo is None
    assert txn["ccy_paid"] == "US Dollar" and txn["fmt"] == "Cheque"
    assert txn["sent_at"] == 1792345678.123


def test_ts_text_has_no_timezone():
    row = make_rows(["2022-09-08T16:12:00"]).iloc[0].to_dict()
    msg = txn_to_message(row, now_sent_at())
    assert msg["ts"] == "2022-09-08T16:12:00"
    assert "Z" not in msg["ts"] and "+" not in msg["ts"]
    assert msg["txn_id"] == "0"
    row["ts"] = pd.Timestamp("2022-09-08T16:12:00", tz="UTC")
    with pytest.raises(ValueError):
        txn_to_message(row, now_sent_at())


def test_sent_at_format():
    assert format_epoch(1792345678.1234) == "1792345678.123"
    assert abs(float(now_sent_at()) - datetime.now().timestamp()) < 5


def test_scorer_gets_the_same_clues_from_raw_and_decoded_messages():
    # The scorer may pass the text fields or message_to_txn's values (ts as a
    # datetime) to ClueBuilder.compute; both must give the same clues.
    raw, decoded = ClueBuilder(), ClueBuilder()
    rows = make_rows(minutes("2022-09-08T16:12:00", 20, step=7)).to_dict("records")
    for row in rows:
        msg = txn_to_message(row, now_sent_at())
        assert raw.compute(msg) == decoded.compute(message_to_txn(msg))


def test_message_to_txn_rejects_missing_and_extra_fields():
    msg = txn_to_message(make_rows(["2022-09-08T16:12:00"]).iloc[0].to_dict(), now_sent_at())
    with pytest.raises(ValueError):
        message_to_txn({k: v for k, v in msg.items() if k != "fmt"})
    with pytest.raises(ValueError):
        message_to_txn(dict(msg, label="1"))


def test_missing_value_is_an_error():
    row = make_rows(["2022-09-08T16:12:00"]).iloc[0].to_dict()
    row["amount_usd"] = float("nan")
    with pytest.raises(ValueError):
        txn_to_message(row, now_sent_at())


# ---------------------------------------------------------------- start and loading

def test_default_start_is_test_start(fixture_files):
    splits = replayer.load_splits(fixture_files["splits"])
    start = choose_start(None, splits)
    assert start == datetime(2022, 9, 8, 16, 12)
    rows = load_rows(fixture_files["clean"], start)
    assert len(rows) == 6
    assert rows["ts"].min() == pd.Timestamp(TEST_START)


def test_start_before_test_needs_the_flag(fixture_files):
    splits = replayer.load_splits(fixture_files["splits"])
    with pytest.raises(ValueError, match="train"):
        choose_start("2022-09-08T16:10:00", splits)
    start = choose_start("2022-09-08T16:10:00", splits, allow_before_test=True)
    assert len(load_rows(fixture_files["clean"], start)) == 8
    # The command line turns the error into a usage error.
    with pytest.raises(SystemExit):
        main(cli(fixture_files, "--start", "2022-09-08T16:10:00", "--dry-run"))


def test_bad_start_format_and_negative_speed(fixture_files):
    splits = replayer.load_splits(fixture_files["splits"])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        choose_start("2022-09-08 16:12", splits)
    with pytest.raises(SystemExit):
        main(cli(fixture_files, "--speed", "-1", "--dry-run"))


def test_sample_data_gives_a_warning(fixture_files, capsys):
    path = fixture_files["splits"]
    data = json.loads(open(path).read())
    data["provenance"]["mode"] = "sample"
    with open(path, "w") as fh:
        json.dump(data, fh)
    replayer.load_splits(path)
    assert "SAMPLE" in capsys.readouterr().err


def test_label_column_is_never_requested(fixture_files, monkeypatch):
    asked = []
    real_read = pd.read_csv

    def spy(path, *args, **kwargs):
        if not kwargs.get("nrows") == 0:          # the header peek reads no rows
            asked.append(kwargs.get("usecols"))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", spy)
    rows = load_rows(fixture_files["clean"], datetime(2022, 9, 8, 16, 12))
    assert asked == [list(TXN_COLUMNS)]
    assert "label" not in rows.columns and "split" not in rows.columns


def test_works_without_a_label_column(tmp_path, fixture_files):
    # A file with no label column at all: reading label would crash.
    df = fixture_files["df"][list(TXN_COLUMNS)]
    path = tmp_path / "no_label.csv"
    write_clean_csv(df, path)
    assert len(load_rows(str(path), datetime(2022, 9, 8, 16, 12))) == 6


def test_dry_run_prints_json_lines(fixture_files, capsys):
    assert main(cli(fixture_files, "--dry-run", "--limit", "3")) is None
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    txns = [message_to_txn(json.loads(line)) for line in lines]
    assert [t["txn_id"] for t in txns] == [2, 3, 4]


# ---------------------------------------------------------------- order and limit

def test_sent_in_ts_then_txn_id_order(fixture_files):
    r = fake_redis()
    main(cli(fixture_files, "--speed", "0"), r=r)
    ids = [int(f["txn_id"]) for _id, f in r.xrange(bus.TXNS)]
    assert ids == [2, 3, 4, 5, 6, 7]     # rows 2 and 3 share a timestamp: by txn_id


def test_tied_timestamps_are_sorted_by_txn_id(tmp_path):
    r = fake_redis()
    df = make_rows(["2022-09-08T16:12:00"] * 3 + ["2022-09-08T16:13:00"])
    df = df.iloc[[2, 0, 3, 1]]           # stored out of order
    path = tmp_path / "c.csv"
    write_clean_csv(df, path)
    rows = load_rows(str(path), datetime(2022, 9, 8, 16, 12))
    replay(r, rows, "t", speed=0)
    assert [int(f["txn_id"]) for _id, f in r.xrange("t")] == [0, 1, 2, 3]


def test_limit(fixture_files):
    r = fake_redis()
    summary = main(cli(fixture_files, "--speed", "0", "--limit", "4"), r=r)
    assert r.xlen(bus.TXNS) == 4
    assert summary["sent"] == 4 and summary["last_txn_id"] == 5


# ---------------------------------------------------------------- pacing

def test_pacing_ten_minutes_at_600_is_one_second():
    r = fake_redis()
    clock = FakeClock()
    rows = make_rows(minutes("2022-09-08T16:12:00", 2, step=10))
    replay(r, rows, "t", speed=600, sleep_fn=clock.sleep, clock_fn=clock.clock)
    assert sum(clock.sleeps) == pytest.approx(1.0)
    assert r.xlen("t") == 2


def test_pacing_has_no_drift():
    # 31 rows one data minute apart at 600x: the last is due 3.0 s after the
    # first. Every clock reading costs 10 ms of "work"; because each wait is
    # measured from the fixed start time, that work does not add up.
    r = fake_redis()
    clock = FakeClock(cost=0.01)
    rows = make_rows(minutes("2022-09-08T16:12:00", 31))
    summary = replay(r, rows, "t", speed=600, sleep_fn=clock.sleep, clock_fn=clock.clock)
    assert 3.0 <= summary["wall_seconds"] < 3.05
    assert summary["data_seconds"] == 30 * 60


def test_same_minute_rows_do_not_sleep():
    r = fake_redis()
    clock = FakeClock()
    rows = make_rows(["2022-09-08T16:12:00"] * 1200)   # more than one batch
    replay(r, rows, "t", speed=600, sleep_fn=clock.sleep, clock_fn=clock.clock)
    assert clock.sleeps == []
    assert r.xlen("t") == 1200


def test_speed_zero_never_sleeps():
    r = fake_redis()
    clock = FakeClock()
    rows = make_rows(minutes("2022-09-08T16:12:00", 50, step=60))
    replay(r, rows, "t", speed=0, sleep_fn=clock.sleep, clock_fn=clock.clock)
    assert clock.sleeps == []
    assert r.xlen("t") == 50


def test_stop_event_stops_after_current_batch():
    r = fake_redis()
    clock = FakeClock()
    stop = threading.Event()
    rows = make_rows(minutes("2022-09-08T16:12:00", 10))

    def sleep_then_stop(seconds):     # "Ctrl+C" during the first wait
        clock.sleep(seconds)
        stop.set()

    summary = replay(r, rows, "t", speed=600, sleep_fn=sleep_then_stop,
                     clock_fn=clock.clock, stop_event=stop)
    assert summary["stopped"] and summary["sent"] == 1 and summary["last_txn_id"] == 0
    assert r.xlen("t") == 1


# ---------------------------------------------------------------- MAXLEN

def test_maxlen_keeps_stream_near_the_cap():
    # MAXLEN ~ 50 is approximate: real Redis only trims whole internal nodes
    # (default 100 entries each), so the stream can be up to about 50 + 100
    # long. fakeredis 2.40 keeps 100 here. Both stay far below 1,000.
    r = fake_redis()
    rows = make_rows(["2022-09-08T16:12:00"] * 1000)
    replay(r, rows, "t", speed=0, maxlen=50)
    assert 50 <= r.xlen("t") <= 150
    newest = r.xrevrange("t", "+", "-", count=1)[0][1]
    assert newest["txn_id"] == "999"


# ---------------------------------------------------------------- resume

def test_resume_continues_without_duplicates(tmp_path):
    r = fake_redis()
    df = make_rows(minutes("2022-09-08T16:12:00", 100))
    path = tmp_path / "clean.csv"
    write_clean_csv(df, path)
    splits = tmp_path / "splits.json"
    splits.write_text(json.dumps({"test_start": TEST_START, "provenance": {"mode": "full"}}))
    args = ["--clean", str(path), "--splits", str(splits), "--speed", "0"]

    main(args + ["--limit", "30"], r=r)
    assert r.xlen(bus.TXNS) == 30
    summary = main(args + ["--resume"], r=r)
    assert summary["first_txn_id"] == 30
    ids = [int(f["txn_id"]) for _id, f in r.xrange(bus.TXNS)]
    assert ids == list(range(100))      # no gaps, no duplicates


def test_resume_on_empty_stream_starts_at_the_beginning(fixture_files):
    r = fake_redis()
    summary = main(cli(fixture_files, "--speed", "0", "--resume"), r=r)
    assert summary["first_txn_id"] == 2 and summary["sent"] == 6


# ---------------------------------------------------------------- end to end

def test_end_to_end_messages_match_the_clean_table(fixture_files):
    r = fake_redis()
    clock = FakeClock()
    summary = main(cli(fixture_files, "--speed", "600"), r=r,
                   sleep_fn=clock.sleep, clock_fn=clock.clock)
    assert summary["sent"] == 6
    # 16:12 -> 17:00 is 48 data minutes = 4.8 s at 600x.
    assert sum(clock.sleeps) == pytest.approx(4.8)

    expected = fixture_files["df"].iloc[2:].reset_index(drop=True)
    messages = r.xrange(bus.TXNS)
    assert len(messages) == len(expected)
    for (_id, fields), (_i, row) in zip(messages, expected.iterrows()):
        assert tuple(fields) == TXN_FIELDS
        txn = message_to_txn(fields)
        for name in TXN_COLUMNS:
            want = row[name]
            if name == "ts":
                want = want.to_pydatetime()
            elif name == "txn_id":
                want = int(want)
            elif name in ("amount_usd", "amt_paid", "amt_recv"):
                want = float(want)
            else:
                want = str(want)
            assert txn[name] == want, name


def test_replays_real_cleaning_pipeline_output(tmp_path):
    """Run scripts/clean_data.py on the pipeline test fixture, then replay its
    output, so a change in the cleaning output format breaks this test."""
    from scripts import clean_data
    from tests.test_pipeline import good_rows, write_csv

    out = tmp_path / "out"
    raw = write_csv(tmp_path / "raw.csv", good_rows())
    assert clean_data.main(["--input", str(raw), "--out", str(out)]) == 0
    clean = read_clean_csv(out / "clean.csv")
    splits = json.loads((out / "splits.json").read_text())
    expected = clean[clean["ts"] >= pd.Timestamp(splits["test_start"])]

    r = fake_redis()
    summary = main(["--clean", str(out / "clean.csv"), "--splits", str(out / "splits.json"),
                    "--speed", "0"], r=r)
    assert summary["sent"] == len(expected) > 0
    sent = [message_to_txn(f) for _id, f in r.xrange(bus.TXNS)]
    assert [t["txn_id"] for t in sent] == expected["txn_id"].tolist()
    assert [t["from_acct"] for t in sent] == expected["from_acct"].tolist()
    assert [t["amount_usd"] for t in sent] == expected["amount_usd"].tolist()

