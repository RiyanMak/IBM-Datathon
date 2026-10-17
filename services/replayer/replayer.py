"""
Step 1 - the replayer.

Reads the cleaned transactions (clean.csv) and plays the test period onto
the Redis stream `txns`, in time order, sped up (default 600x: one data minute
every 0.1 real seconds). The scorer reads that stream.

How to run (from the repo root):
  python -m services.replayer.main --clean data/processed/v1/clean.csv \\
      --splits data/processed/v1/splits.json --dry-run          # no Redis: prints messages
  python -m services.replayer.main                               # the real service (Docker)
  python -m services.replayer.main --resume                      # continue after Ctrl+C

Redis host and stream names come from common/bus.py (REDIS_HOST, default "redis").
The message format is in common/streams.py.

Only rows with ts >= --start are sent (default: the test start from
splits.json). The label column is never read.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

from common import bus
from common.streams import (
    TS_FORMAT_ISO,
    TXN_COLUMNS,
    message_to_txn,
    now_sent_at,
    txn_to_message,
)

DEFAULT_SPEED = 600.0      # data seconds per real second
DEFAULT_MAXLEN = 200000    # approximate cap on the stream length (plan, D5)
BATCH_SIZE = 500           # XADDs per Redis pipeline round trip
PROGRESS_EVERY = 10000     # print a progress line every this many messages
DRY_RUN_COUNT = 5          # messages printed by --dry-run
MAX_SLEEP_STEP = 0.5       # sleep in short steps so Ctrl+C is noticed quickly


def log(*parts):
    """Status lines go to stderr, so --dry-run's stdout is only JSON lines."""
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- loading

def load_splits(path):
    with open(path) as fh:
        splits = json.load(fh)
    mode = splits.get("provenance", {}).get("mode")
    if mode != "full":
        log("WARNING: %s says provenance.mode = %r, not 'full'. This is a SAMPLE of the "
            "data and its txn_ids are provisional; they will not match the full dataset."
            % (path, mode))
    return splits


def parse_ts(text):
    try:
        return datetime.strptime(text, TS_FORMAT_ISO)
    except ValueError:
        raise ValueError("timestamp %r is not in the format YYYY-MM-DDTHH:MM:SS "
                         "(for example 2022-09-08T16:12:00)" % text) from None


def choose_start(start_text, splits, allow_before_test=False):
    """The first data timestamp to send. Default: the test start from splits.json."""
    test_start = parse_ts(splits["test_start"])
    if start_text is None:
        return test_start
    start = parse_ts(start_text)
    if start < test_start and not allow_before_test:
        raise ValueError(
            "--start %s is before the test period, which begins at %s. Rows before that "
            "were used to train and tune the model, so replaying them would make the "
            "scores look better than they really are. Use a later --start, or add "
            "--allow-before-test if you really want this (for example for a demo)."
            % (start_text, splits["test_start"]))
    return start


def load_rows(clean_path, start):
    """Read the transaction columns for rows with ts >= start, sorted by (ts, txn_id).

    Only TXN_COLUMNS are read; label, split and the rest stay on disk.
    """
    import pandas as pd
    from common.clean_transactions import read_clean_csv

    df = read_clean_csv(clean_path, columns=TXN_COLUMNS)
    df = df[df["ts"] >= pd.Timestamp(start)]
    df = df.sort_values(["ts", "txn_id"], kind="mergesort").reset_index(drop=True)
    check_order(df)
    return df


def check_order(df):
    """Make sure the rows are in time order and txn_id goes up with them.

    The cleaning pipeline numbers rows 0..N-1 in time order, so txn_id must
    be strictly increasing. --resume relies on that.
    """
    if df["ts"].isna().any():
        raise ValueError("ts has missing values")
    if not df["ts"].is_monotonic_increasing:
        raise ValueError("rows are not in time order")
    ids = df["txn_id"]
    if len(ids) > 1 and not (ids.diff().iloc[1:] > 0).all():
        raise ValueError("txn_id does not increase with (ts, txn_id); "
                         "clean.csv does not look like the cleaning pipeline's output")


def skip_already_sent(df, last_txn_id):
    """Drop rows that a previous run already sent (txn_id <= last_txn_id)."""
    if last_txn_id is None:
        return df
    return df[df["txn_id"] > last_txn_id].reset_index(drop=True)


# ---------------------------------------------------------------- Redis

def last_sent(r, stream):
    """The decoded last message on the stream, or None if the stream is empty."""
    newest = r.xrevrange(stream, "+", "-", count=1)
    if not newest:
        return None
    _msg_id, fields = newest[0]
    return message_to_txn(fields)


def send_batch(r, stream, rows, maxlen):
    """XADD a list of row dicts in one pipeline. sent_at is stamped here."""
    pipe = r.pipeline(transaction=False)
    for row in rows:
        msg = txn_to_message(row, now_sent_at())
        if maxlen:
            pipe.xadd(stream, msg, maxlen=maxlen, approximate=True)
        else:
            pipe.xadd(stream, msg)
    pipe.execute()


# ---------------------------------------------------------------- replay

def replay(r, rows_df, stream=bus.TXNS, speed=DEFAULT_SPEED, limit=None,
           maxlen=DEFAULT_MAXLEN, sleep_fn=time.sleep, clock_fn=time.monotonic,
           stop_event=None):
    """Send rows_df (already sorted) to the stream, paced by data time.

    Pacing has no drift: row i is due at wall0 + (ts_i - ts_0) / speed, where
    wall0 is the clock when we start. All rows with the same ts are sent
    together, in pipeline batches of at most BATCH_SIZE. speed=0 means send as
    fast as possible (never sleep).

    sleep_fn and clock_fn can be replaced in tests. If stop_event (a
    threading.Event) is set, the replay stops after the current batch.

    Returns a summary dict.
    """
    if speed < 0:
        raise ValueError("speed must be 0 or more")
    if limit is not None:
        rows_df = rows_df.iloc[:limit]

    summary = {"sent": 0, "first_txn_id": None, "last_txn_id": None,
               "first_ts": None, "last_ts": None, "data_seconds": 0.0,
               "wall_seconds": 0.0, "slept_seconds": 0.0, "stopped": False}
    n = len(rows_df)
    if n == 0:
        return summary

    # Plain Python lists are much faster to loop over than DataFrame rows.
    cols = {name: rows_df[name].tolist() for name in TXN_COLUMNS}
    # Data time of each row, in seconds after the first row.
    ts_seconds = (rows_df["ts"] - rows_df["ts"].iloc[0]).dt.total_seconds().tolist()

    wall0 = clock_fn()
    pending = []          # rows waiting to be sent
    next_progress = PROGRESS_EVERY

    def stopping():
        return stop_event is not None and stop_event.is_set()

    def flush():
        nonlocal next_progress
        if not pending:
            return
        send_batch(r, stream, pending, maxlen)
        if summary["first_txn_id"] is None:
            summary["first_txn_id"] = pending[0]["txn_id"]
            summary["first_ts"] = pending[0]["ts"]
        summary["last_txn_id"] = pending[-1]["txn_id"]
        summary["last_ts"] = pending[-1]["ts"]
        summary["sent"] += len(pending)
        pending.clear()
        if summary["sent"] >= next_progress:
            elapsed = clock_fn() - wall0
            rate = summary["sent"] / elapsed if elapsed > 0 else float("inf")
            log("sent %d | data time %s | %.0f msg/s"
                % (summary["sent"], summary["last_ts"].strftime(TS_FORMAT_ISO), rate))
            next_progress += PROGRESS_EVERY

    for i in range(n):
        if stopping():
            break
        # First row of a new timestamp: wait until that data time is due.
        if speed > 0 and (i == 0 or ts_seconds[i] != ts_seconds[i - 1]):
            due = wall0 + ts_seconds[i] / speed
            if due > clock_fn():
                flush()   # send what is ready before waiting
                while not stopping():
                    wait = due - clock_fn()
                    if wait <= 0:
                        break
                    step = min(wait, MAX_SLEEP_STEP)
                    summary["slept_seconds"] += step
                    sleep_fn(step)
                if stopping():
                    break
        pending.append({name: cols[name][i] for name in TXN_COLUMNS})
        if len(pending) >= BATCH_SIZE:
            flush()

    if not stopping():
        flush()
    else:
        pending.clear()   # rows not sent yet; --resume will send them
        summary["stopped"] = True

    summary["wall_seconds"] = clock_fn() - wall0
    if summary["first_ts"] is not None:
        summary["data_seconds"] = (summary["last_ts"] - summary["first_ts"]).total_seconds()
    return summary


def print_summary(summary):
    if summary["sent"] == 0:
        log("nothing sent")
        return
    wall = summary["wall_seconds"]
    rate = summary["sent"] / wall if wall > 0 else float("inf")
    log("summary: sent %d messages, txn_id %s..%s, data time %s .. %s (%.1f data hours) "
        "in %.1f s wall time (%.0f msg/s)"
        % (summary["sent"], summary["first_txn_id"], summary["last_txn_id"],
           summary["first_ts"].strftime(TS_FORMAT_ISO), summary["last_ts"].strftime(TS_FORMAT_ISO),
           summary["data_seconds"] / 3600,
           wall, rate))


# ---------------------------------------------------------------- command line

def default_speed():
    text = os.environ.get("SPEED")
    return float(text) if text else DEFAULT_SPEED


def build_parser():
    p = argparse.ArgumentParser(description="Mule Ring Hunter replayer: clean.csv -> Redis stream")
    p.add_argument("--clean", default=os.environ.get("CLEAN_PATH", "/data/clean.csv"),
                   help="cleaned transactions (default: $CLEAN_PATH or /data/clean.csv)")
    p.add_argument("--splits", default=os.environ.get("SPLITS_PATH", "/data/splits.json"),
                   help="split cut-offs (default: $SPLITS_PATH or /data/splits.json)")
    p.add_argument("--start", help="first data time to send, YYYY-MM-DDTHH:MM:SS "
                                   "(default: test_start from splits.json)")
    p.add_argument("--allow-before-test", action="store_true",
                   help="allow a --start before the test period (those rows were used for training)")
    p.add_argument("--speed", type=float, default=None,
                   help="data seconds per real second (default: $SPEED or 600); 0 = as fast as possible")
    p.add_argument("--limit", type=int, help="send at most this many messages")
    p.add_argument("--resume", action="store_true",
                   help="continue after the last txn_id already on the stream")
    p.add_argument("--dry-run", action="store_true",
                   help="print the first few messages as JSON lines and stop (no Redis needed)")
    p.add_argument("--stream", default=bus.TXNS, help="stream name (default: %(default)s)")
    p.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN,
                   help="approximate cap on the stream length (default: %(default)s; 0 = no cap)")
    return p


def main(argv=None, r=None, sleep_fn=time.sleep, clock_fn=time.monotonic):
    """Run the replayer. Tests pass argv, a fake Redis r and a fake clock."""
    p = build_parser()
    args = p.parse_args(argv)
    speed = args.speed if args.speed is not None else default_speed()
    if speed < 0:
        p.error("--speed must be 0 or more (0 = no waiting)")
    if args.limit is not None and args.limit < 0:
        p.error("--limit must be 0 or more")
    if args.maxlen < 0:
        p.error("--maxlen must be 0 or more")

    splits = load_splits(args.splits)
    try:
        start = choose_start(args.start, splits, args.allow_before_test)
    except ValueError as exc:
        p.error(str(exc))

    t0 = time.monotonic()
    rows = load_rows(args.clean, start)
    log("loaded %d rows with ts >= %s from %s in %.1f s"
        % (len(rows), start.strftime(TS_FORMAT_ISO), args.clean, time.monotonic() - t0))

    if args.dry_run:
        count = DRY_RUN_COUNT if args.limit is None else min(args.limit, DRY_RUN_COUNT)
        for row in rows.head(count).to_dict("records"):
            print(json.dumps(txn_to_message(row, now_sent_at())))
        log("dry run: nothing was sent")
        return None

    r = r or bus.connect()
    if args.resume:
        last = last_sent(r, args.stream)
        if last is None:
            log("--resume: stream '%s' is empty, starting from the beginning" % args.stream)
        else:
            rows = skip_already_sent(rows, last["txn_id"])
            log("--resume: last message on '%s' is txn_id %d (data time %s); %d rows left"
                % (args.stream, last["txn_id"], last["ts"].strftime(TS_FORMAT_ISO), len(rows)))
    else:
        existing = r.xlen(args.stream)
        if existing:
            log("WARNING: stream '%s' already has %d messages. Use --resume to continue "
                "without duplicates." % (args.stream, existing))

    log("replaying %d rows onto '%s' at speed %g (%s), maxlen ~%s"
        % (len(rows) if args.limit is None else min(len(rows), args.limit), args.stream, speed,
           "no waiting" if speed == 0 else "%g data seconds per second" % speed,
           args.maxlen or "none"))

    # Ctrl+C: finish the current batch, then stop. A second Ctrl+C stops at once.
    stop = threading.Event()
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        def on_ctrl_c(_signum, _frame):
            if stop.is_set():
                raise KeyboardInterrupt
            log("\nCtrl+C: stopping after the current batch (press again to quit at once)")
            stop.set()
        old_handler = signal.signal(signal.SIGINT, on_ctrl_c)
    try:
        summary = replay(r, rows, args.stream, speed, args.limit, args.maxlen,
                         sleep_fn, clock_fn, stop_event=stop)
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)

    print_summary(summary)
    if summary["stopped"]:
        log("stopped early. Last sent txn_id: %s. Run again with --resume to continue."
            % summary["last_txn_id"])
    return summary
