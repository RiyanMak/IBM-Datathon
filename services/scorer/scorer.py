"""
Steps 2 and 3 - the scorer.

Reads transfers from the Redis stream `txns` (written by the replayer), computes
the 32 behaviour clues with the shared ClueBuilder, scores each transfer with the
XGBoost model and writes the result to the stream `scored` (read by the ring
service and the investigator page).

How to run (from the repo root):
  python -m services.scorer.main                     # the real service (Docker)
  python -m services.scorer.main --once              # score what is on the stream now, then exit
  python -m services.scorer.main --no-warmup --once  # laptop without clean.csv

Startup, in order:
  1. load models/scorer.json, feature_list.json and thresholds.json. It refuses
     to start if the model's feature order differs from common/features.py:
     a wrong order gives wrong scores with no error.
  2. warm up: every clean.csv row before the test start goes through the
     ClueBuilder, so each account has its history when the first live message
     arrives (otherwise every account would look brand new).
  3. continuity: after a restart, the `txns` messages this consumer group has
     already handled go through the ClueBuilder as well, so the memory has no gap.
  4. loop: XREADGROUP a batch from `txns`, score it, XADD to `scored`, update the
     `stats:scorer` hash, XACK.

Per-transfer latency is scored_at - sent_at (both wall-clock seconds); p50 and p99
are printed and kept in stats:scorer. The top-3 SHAP reasons are computed only for
transfers at or above the ring threshold: those are the only ones the ring
service and the page show, and SHAP is the slow part.

Scaling: `docker compose up -d --scale scorer=3` runs three consumers in the same
group and Redis splits the stream between them. Each scorer then sees only its
share of the transfers, so its per-account memory is partial and the clues drift
from the offline ones. Use one scorer for accuracy numbers, three to show throughput.
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from common import bus
from common.features import FEATURE_NAMES, ClueBuilder, to_minutes
from common.paths import DATA_DIR, MODELS_DIR
from common.streams import TXN_FIELDS, message_to_txn, now_sent_at
from services.scorer.reasons import phrases

GROUP = "scorers"
STATS_KEY = "stats:scorer"
BATCH = 200             # messages per XREADGROUP
BLOCK_MS = 1000         # never read without block=: an unblocked loop spins at 100 % CPU
DEFAULT_MAXLEN = 300_000  # approximate cap on the scored stream
LATENCY_WINDOW = 10_000   # latencies kept for the percentiles
PROGRESS_EVERY = 10_000
WARMUP_CHUNK = 500_000    # clean.csv rows per chunk during warm-up (keeps memory flat)
STREAM_PAGE = 5_000       # messages per XRANGE during stream warm-up
CLUE_DECIMALS = 4


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- model

class Model:
    """The XGBoost scorer plus its feature order and thresholds, loaded from one folder."""

    def __init__(self, models_dir, nthread=1):
        import xgboost as xgb   # imported here so the module loads where xgboost is absent (tests of helpers)

        models_dir = Path(models_dir)
        self.features = list(json.loads((models_dir / "feature_list.json").read_text()))
        if self.features != list(FEATURE_NAMES):
            raise ValueError(
                "%s/feature_list.json does not match FEATURE_NAMES in common/features.py. "
                "Retrain the model or restore the clue list; with a different order the "
                "scores would be wrong without any error." % models_dir)
        thresholds = json.loads((models_dir / "thresholds.json").read_text())
        self.alert = float(thresholds["alert"])
        self.ring = float(thresholds["ring"])
        if not 0.0 <= self.ring <= self.alert <= 1.0:
            raise ValueError("thresholds.json must have 0 <= ring <= alert <= 1, got ring=%s alert=%s"
                             % (self.ring, self.alert))
        self.booster = xgb.Booster()
        self.booster.load_model(str(models_dir / "scorer.json"))
        self.booster.set_param({"nthread": int(nthread)})
        self._xgb = xgb

    def _matrix(self, X):
        return self._xgb.DMatrix(np.asarray(X, dtype=np.float32), feature_names=self.features)

    def scores(self, X):
        """Laundering probability per row of X (n x 32)."""
        return self.booster.predict(self._matrix(X))

    def contributions(self, X):
        """SHAP contribution of every clue per row (n x 32); the bias column is dropped."""
        return self.booster.predict(self._matrix(X), pred_contribs=True)[:, :-1]


# ---------------------------------------------------------------- warm-up

def feed_row(builder, ts, from_acct, to_acct, amount_usd, ccy_paid, ccy_recv, fmt):
    """One transfer into the builder's memory; returns its clue row."""
    return builder.compute_row(to_minutes(ts) if not isinstance(ts, (int, np.integer)) else int(ts),
                               from_acct, to_acct, amount_usd, ccy_paid, ccy_recv, fmt)


def warm_up_from_file(builder, clean_path, until, chunk_rows=WARMUP_CHUNK, progress=log):
    """Feed every clean.csv row with ts < until (ISO text or Timestamp) through the builder.

    The file is sorted by time, so the rows to feed are a prefix; it is read in
    chunks so memory stays flat. Only the clue columns are read; the label never is.
    Returns the number of rows fed.
    """
    import pandas as pd

    usecols = ["ts", "from_acct", "to_acct", "amount_usd", "ccy_paid", "ccy_recv", "fmt"]
    dtype = {"from_acct": str, "to_acct": str, "ccy_paid": str, "ccy_recv": str, "fmt": str,
             "amount_usd": "float64"}
    until_min = to_minutes(pd.Timestamp(until))
    fed = 0
    t0 = time.monotonic()
    for chunk in pd.read_csv(clean_path, usecols=usecols, dtype=dtype, na_filter=False,
                             chunksize=chunk_rows, float_precision="round_trip"):
        ts = pd.to_datetime(chunk["ts"], format="ISO8601").values.astype("datetime64[m]").astype("int64")
        n = int((ts < until_min).sum())
        rows = zip(ts[:n], chunk["from_acct"].values[:n], chunk["to_acct"].values[:n],
                   chunk["amount_usd"].values[:n], chunk["ccy_paid"].values[:n],
                   chunk["ccy_recv"].values[:n], chunk["fmt"].values[:n])
        for t, fa, ta, amt, cp, cr, f in rows:
            builder.compute_row(int(t), fa, ta, float(amt), cp, cr, f)
        fed += n
        if progress:
            progress("warm-up: %d rows in %.0f s" % (fed, time.monotonic() - t0))
        if n < len(chunk):
            break
    return fed


def _next_id(msg_id: str) -> str:
    ms, seq = msg_id.split("-")
    return "%s-%d" % (ms, int(seq) + 1)


def warm_up_from_stream(builder, r, stream=bus.TXNS, group=GROUP):
    """After a restart: feed the messages this group already handled through the builder.

    Everything up to the group's last delivered id goes in, except messages that are
    still pending (not acknowledged): those will be delivered again and scored, so
    feeding them here would count them twice. Bounded by what the stream still holds.
    Returns the number of messages fed.
    """
    try:
        groups = {g["name"]: g for g in r.xinfo_groups(stream)}
    except Exception:   # stream does not exist yet
        return 0
    info = groups.get(group)
    if not info:
        return 0
    last = str(info.get("last-delivered-id", "0-0"))
    if last in ("0-0", "0"):
        return 0
    pending = r.xpending(stream, group)
    end = last
    inclusive = True
    if pending and pending.get("pending"):
        end = str(pending["min"])
        inclusive = False
    fed = 0
    start = "-"
    while True:
        batch = r.xrange(stream, start, end, count=STREAM_PAGE)
        for msg_id, fields in batch:
            if not inclusive and msg_id == end:
                break
            try:
                txn = message_to_txn(fields)
            except ValueError:
                continue
            feed_row(builder, txn["ts"], txn["from_acct"], txn["to_acct"], txn["amount_usd"],
                     txn["ccy_paid"], txn["ccy_recv"], txn["fmt"])
            fed += 1
        if len(batch) < STREAM_PAGE or batch[-1][0] == end:
            break
        start = _next_id(batch[-1][0])
    return fed


# ---------------------------------------------------------------- scoring

class Stats:
    def __init__(self, window=LATENCY_WINDOW):
        self.scored = 0
        self.flagged = 0
        self.candidates = 0     # at or above the ring threshold
        self.skipped = 0        # malformed messages
        self.latencies = deque(maxlen=window)
        self.started = time.monotonic()

    def percentile(self, pct):
        return float(np.percentile(self.latencies, pct)) if self.latencies else None

    def line(self):
        p50, p99 = self.percentile(50), self.percentile(99)
        elapsed = time.monotonic() - self.started
        rate = self.scored / elapsed if elapsed > 0 else 0.0
        return ("scored %d | flagged %d | ring candidates %d | skipped %d | latency p50 %s ms p99 %s ms | %.0f msg/s"
                % (self.scored, self.flagged, self.candidates, self.skipped,
                   "-" if p50 is None else "%.1f" % p50, "-" if p99 is None else "%.1f" % p99, rate))


def score_batch(model, builder, entries, stats=None):
    """Score one XREADGROUP batch, in stream order.

    entries: [(msg_id, fields)]. Returns [(msg_id, out_fields or None, latency_ms or None)];
    a malformed message gives None so it is acknowledged but not written.
    """
    rows, txns, keep = [], [], []
    for msg_id, fields in entries:
        try:
            txn = message_to_txn(fields)
        except ValueError as exc:
            if stats:
                stats.skipped += 1
            log("skipping malformed txns message %s: %s" % (msg_id, exc))
            keep.append(False)
            continue
        rows.append(feed_row(builder, txn["ts"], txn["from_acct"], txn["to_acct"], txn["amount_usd"],
                             txn["ccy_paid"], txn["ccy_recv"], txn["fmt"]))
        txns.append(txn)
        keep.append(True)

    results = []
    if not rows:
        return [(msg_id, None, None) for msg_id, _ in entries]

    X = np.asarray(rows, dtype=np.float32)
    scores = model.scores(X)
    candidates = np.flatnonzero(scores >= model.ring)
    contribs = model.contributions(X[candidates]) if len(candidates) else None
    contrib_of = {int(i): j for j, i in enumerate(candidates)}
    scored_at = now_sent_at()
    scored_at_f = float(scored_at)

    k = 0
    for (msg_id, fields), ok in zip(entries, keep):
        if not ok:
            results.append((msg_id, None, None))
            continue
        score = float(scores[k])
        out = {name: fields[name] for name in TXN_FIELDS}
        out["score"] = "%.6f" % score
        out["flagged"] = "1" if score >= model.alert else "0"
        reasons = []
        if k in contrib_of:
            reasons = phrases(X[k], contribs[contrib_of[k]], txns[k]["amount_usd"])
        out["reasons"] = json.dumps(reasons)
        out["clues"] = json.dumps({name: round(float(v), CLUE_DECIMALS) for name, v in zip(FEATURE_NAMES, X[k])})
        out["scored_at"] = scored_at
        latency = None
        try:
            latency = (scored_at_f - float(fields["sent_at"])) * 1000.0
        except (KeyError, TypeError, ValueError):
            pass
        if stats:
            stats.scored += 1
            stats.flagged += out["flagged"] == "1"
            stats.candidates += k in contrib_of
            if latency is not None:
                stats.latencies.append(latency)
        results.append((msg_id, out, latency))
        k += 1
    return results


def write_batch(r, results, stats, stream=bus.TXNS, out_stream=bus.SCORED, maxlen=DEFAULT_MAXLEN,
                consumer=""):
    """XADD every scored message, update stats:scorer, then XACK the whole batch, in one pipeline."""
    pipe = r.pipeline(transaction=False)
    n_scored = n_flagged = 0
    for _msg_id, out, _latency in results:
        if out is None:
            continue
        if maxlen:
            pipe.xadd(out_stream, out, maxlen=maxlen, approximate=True)
        else:
            pipe.xadd(out_stream, out)
        n_scored += 1
        n_flagged += out["flagged"] == "1"
    if n_scored:
        pipe.hincrby(STATS_KEY, "scored", n_scored)
        pipe.hincrby(STATS_KEY, "flagged", n_flagged)
    p50, p99 = stats.percentile(50), stats.percentile(99)
    pipe.hset(STATS_KEY, mapping={
        "p50_ms": "" if p50 is None else "%.1f" % p50,
        "p99_ms": "" if p99 is None else "%.1f" % p99,
        "skipped": str(stats.skipped),
        "updated_at": now_sent_at(),
        "consumer": consumer,
    })
    pipe.xack(stream, GROUP, *[msg_id for msg_id, _, _ in results])
    pipe.execute()


def ensure_group(r, stream=bus.TXNS, group=GROUP):
    """Create the consumer group from the start of the stream; fine if it already exists."""
    import redis

    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def run(r, model, builder, consumer, stream=bus.TXNS, out_stream=bus.SCORED, batch=BATCH,
        block_ms=BLOCK_MS, maxlen=DEFAULT_MAXLEN, once=False, max_messages=None, stop_event=None,
        progress=log):
    """The scoring loop. Returns the Stats.

    First the messages already delivered to this consumer but never acknowledged
    (a crash mid-batch), then new ones. With once=True it stops at the first empty
    read; with max_messages it stops after that many scored messages.
    """
    ensure_group(r, stream)
    stats = Stats()
    read_id = "0"         # "0": my pending messages; ">": new messages
    next_progress = PROGRESS_EVERY

    def stopping():
        return stop_event is not None and stop_event.is_set()

    while not stopping():
        resp = r.xreadgroup(GROUP, consumer, {stream: read_id}, count=batch,
                            block=None if read_id == "0" else block_ms)
        entries = resp[0][1] if resp else []
        if not entries:
            if read_id == "0":
                read_id = ">"
                continue
            if once:
                break
            continue
        results = score_batch(model, builder, entries, stats)
        write_batch(r, results, stats, stream, out_stream, maxlen, consumer)
        if progress and stats.scored >= next_progress:
            progress(stats.line())
            next_progress += PROGRESS_EVERY
        if max_messages is not None and stats.scored >= max_messages:
            break
    return stats


# ---------------------------------------------------------------- command line

def build_parser():
    p = argparse.ArgumentParser(description="Mule Ring Hunter scorer: txns -> clues -> XGBoost -> scored")
    p.add_argument("--models", default=os.environ.get("MODELS_PATH", str(MODELS_DIR)),
                   help="folder with scorer.json, feature_list.json, thresholds.json (default: %(default)s)")
    p.add_argument("--clean", default=os.environ.get("CLEAN_PATH", str(DATA_DIR / "clean.csv")),
                   help="clean.csv for the warm-up (default: $CLEAN_PATH or %(default)s)")
    p.add_argument("--splits", default=os.environ.get("SPLITS_PATH", str(DATA_DIR / "splits.json")),
                   help="splits.json; the warm-up feeds rows before its test_start (default: %(default)s)")
    p.add_argument("--warmup-until", help="feed clean.csv rows before this time instead, YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--no-warmup", action="store_true", help="skip the clean.csv warm-up (tests, laptops)")
    p.add_argument("--no-stream-warmup", action="store_true",
                   help="skip feeding already-handled txns messages after a restart")
    p.add_argument("--consumer", default=os.environ.get("SCORER_NAME") or socket.gethostname(),
                   help="consumer name in the group (default: $SCORER_NAME or the host name)")
    p.add_argument("--batch", type=int, default=BATCH, help="messages per read (default: %(default)s)")
    p.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN,
                   help="approximate cap on the scored stream (default: %(default)s; 0 = no cap)")
    p.add_argument("--stream", default=bus.TXNS, help="input stream (default: %(default)s)")
    p.add_argument("--out-stream", default=bus.SCORED, help="output stream (default: %(default)s)")
    p.add_argument("--nthread", type=int, default=int(os.environ.get("NTHREAD", "1")),
                   help="XGBoost threads (default: $NTHREAD or 1; a batch of 200 rows needs no more)")
    p.add_argument("--once", action="store_true", help="score what is on the stream now, then exit")
    p.add_argument("--max-messages", type=int, help="stop after scoring this many messages")
    return p


def main(argv=None, r=None):
    """Run the scorer. Tests pass argv and a fake Redis."""
    p = build_parser()
    args = p.parse_args(argv)
    if args.batch < 1 or args.maxlen < 0:
        p.error("--batch must be at least 1 and --maxlen 0 or more")

    t0 = time.monotonic()
    model = Model(args.models, nthread=args.nthread)
    log("model loaded from %s: %d clues, alert %.4f, ring %.4f" % (args.models, len(model.features),
                                                                   model.alert, model.ring))
    builder = ClueBuilder()

    if not args.no_warmup:
        until = args.warmup_until
        if until is None:
            with open(args.splits) as fh:
                until = json.load(fh)["test_start"]
        fed = warm_up_from_file(builder, args.clean, until)
        log("warm-up: %d rows before %s from %s in %.0f s (%d accounts)"
            % (fed, until, args.clean, time.monotonic() - t0, len(builder._accts)))

    r = r or bus.connect()
    ensure_group(r, args.stream)
    if not args.no_stream_warmup:
        fed = warm_up_from_stream(builder, r, args.stream)
        if fed:
            log("continuity: fed %d already-handled messages from '%s' into memory" % (fed, args.stream))

    log("scoring '%s' -> '%s' as consumer '%s' in group '%s', batch %d, maxlen ~%s"
        % (args.stream, args.out_stream, args.consumer, GROUP, args.batch, args.maxlen or "none"))

    stop = threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def on_signal(_signum, _frame):
            if stop.is_set():
                raise KeyboardInterrupt
            log("\nstopping after the current batch (signal again to quit at once)")
            stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, on_signal)
    try:
        stats = run(r, model, builder, args.consumer, args.stream, args.out_stream, args.batch,
                    BLOCK_MS, args.maxlen, args.once, args.max_messages, stop_event=stop)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    log("done: " + stats.line())
    return stats


if __name__ == "__main__":
    main()
