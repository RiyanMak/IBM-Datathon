"""How fast can the replayer write to Redis? One reproducible messages-per-second number.

Runs the real replayer (services.replayer.replayer.replay) with no pacing (speed 0) into a
short-lived stream with a small length cap, times it, prints one line and deletes the
stream again. Nothing reads the stress stream, so the scorer and the live `txns` stream
are untouched; the number is the replayer plus Redis, not the whole pipeline.

Usage (from the repo root; on the VM inside the compose image, which has Redis next to it):
  docker compose run --rm scorer python -m scripts.stress_replayer --clean /data/data/clean.csv \\
      --splits /data/data/splits.json
  python -m scripts.stress_replayer --limit 20000 --keep      # keep the stream to look at it

Prints: "N messages in S s = X msg/s".
"""
import argparse
import os
import sys
import time

from common import bus
from common.paths import DATA_DIR
from services.replayer.replayer import choose_start, load_rows, load_splits, replay

DEFAULT_LIMIT = 100000
DEFAULT_STREAM = "txns_stress"
DEFAULT_MAXLEN = 20000


def stress(r, rows, stream=DEFAULT_STREAM, limit=DEFAULT_LIMIT, maxlen=DEFAULT_MAXLEN, keep=False,
           clock_fn=time.monotonic):
    """Send up to `limit` rows as fast as possible and time it. Returns (sent, seconds, msg/s).

    The stress stream is emptied first (a leftover from an earlier run would only add to
    its length) and deleted at the end unless keep is True.
    """
    if stream == bus.TXNS:
        raise ValueError("refusing to stress the live stream %r; use a scratch name such as %r"
                         % (bus.TXNS, DEFAULT_STREAM))
    r.delete(stream)
    try:
        t0 = clock_fn()
        summary = replay(r, rows, stream=stream, speed=0, limit=limit, maxlen=maxlen, clock_fn=clock_fn)
        seconds = clock_fn() - t0
    finally:
        if not keep:
            r.delete(stream)
    sent = summary["sent"]
    rate = sent / seconds if seconds > 0 else float("inf")
    return sent, seconds, rate


def result_line(sent, seconds, rate):
    return "%d messages in %.2f s = %.0f msg/s" % (sent, seconds, rate)


def main(argv=None, r=None, rows=None):
    """Command line. Tests pass a fake Redis r and a small rows DataFrame."""
    p = argparse.ArgumentParser(description="Time the replayer at full speed into a scratch stream")
    p.add_argument("--clean", default=os.environ.get("CLEAN_PATH", str(DATA_DIR / "clean.csv")))
    p.add_argument("--splits", default=os.environ.get("SPLITS_PATH", str(DATA_DIR / "splits.json")))
    p.add_argument("--start", help="first data time to send (default: test_start from splits.json)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="messages to send (default: %(default)s)")
    p.add_argument("--stream", default=DEFAULT_STREAM, help="scratch stream (default: %(default)s)")
    p.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN,
                   help="approximate cap on the scratch stream (default: %(default)s; 0 = none)")
    p.add_argument("--keep", action="store_true", help="do not delete the stream at the end")
    args = p.parse_args(argv)
    if args.limit < 1:
        p.error("--limit must be 1 or more")
    if args.maxlen < 0:
        p.error("--maxlen must be 0 or more")
    if args.stream == bus.TXNS:
        p.error("refusing to stress the live stream %r" % bus.TXNS)

    if rows is None:
        splits = load_splits(args.splits)
        try:
            start = choose_start(args.start, splits)
        except ValueError as exc:
            p.error(str(exc))
        t0 = time.monotonic()
        rows = load_rows(args.clean, start)
        print("loaded %d rows in %.1f s (not counted in the timing)" % (len(rows), time.monotonic() - t0),
              file=sys.stderr)
    r = r or bus.connect()
    sent, seconds, rate = stress(r, rows, args.stream, args.limit, args.maxlen, args.keep)
    print(result_line(sent, seconds, rate))
    if args.keep:
        print("kept stream %r (%d messages); delete it with DEL %s" % (args.stream, r.xlen(args.stream), args.stream),
              file=sys.stderr)
    return sent, seconds, rate


if __name__ == "__main__":
    main()
