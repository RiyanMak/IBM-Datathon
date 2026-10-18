"""How many messages per second can the replayer push into Redis on this machine?

Runs the replayer at speed 0 (no pacing) into a scratch stream with a short cap,
several times, and prints messages per second for each run and the median. The
number quoted in the pitch is the one measured on the VM, not on a laptop.

From the repo root, against the stack's Redis (inside the compose image on the VM):
  docker compose run --rm scorer python -m scripts.stress_replayer --limit 200000
On a laptop with a local Redis:
  REDIS_HOST=localhost python -m scripts.stress_replayer --clean data/processed/clean.csv \\
      --splits data/processed/splits.json --limit 100000

The scratch stream is deleted afterwards, so `txns` and the scorer are untouched.
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime

from common import bus
from common.paths import DATA_DIR
from services.replayer.replayer import load_rows, replay

STREAM = "stress"
DEFAULT_LIMIT = 200000
DEFAULT_MAXLEN = 20000


def run(r, rows, repeat, stream=STREAM, maxlen=DEFAULT_MAXLEN, clock_fn=time.monotonic) -> dict:
    """Replay `rows` `repeat` times at full speed; returns per-run rates and the median."""
    rates = []
    for i in range(repeat):
        r.delete(stream)
        summary = replay(r, rows, stream=stream, speed=0, maxlen=maxlen, clock_fn=clock_fn)
        rate = summary["sent"] / summary["wall_seconds"] if summary["wall_seconds"] > 0 else float("inf")
        rates.append(rate)
        print("run %d: %d messages in %.2f s = %.0f msg/s" % (i + 1, summary["sent"], summary["wall_seconds"], rate),
              file=sys.stderr)
    r.delete(stream)
    return {"messages": int(len(rows)), "runs": repeat, "rates": rates,
            "median_msg_per_s": statistics.median(rates), "stream_cap": maxlen,
            "host": bus.redis_host(), "measured_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def main(argv=None, r=None):
    p = argparse.ArgumentParser(description="Replayer throughput into Redis, at speed 0")
    p.add_argument("--clean", default=os.environ.get("CLEAN_PATH", str(DATA_DIR / "clean.csv")))
    p.add_argument("--splits", default=os.environ.get("SPLITS_PATH", str(DATA_DIR / "splits.json")))
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="messages per run (default %(default)s)")
    p.add_argument("--repeat", type=int, default=3, help="runs (default %(default)s); the median is reported")
    p.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN, help="cap on the scratch stream")
    p.add_argument("--stream", default=STREAM, help="scratch stream name (deleted afterwards)")
    p.add_argument("--out", help="write the result as JSON here")
    args = p.parse_args(argv)
    if args.stream in bus.STREAMS:
        p.error("--stream must not be one of the real streams %s" % (bus.STREAMS,))

    with open(args.splits) as fh:
        start = datetime.strptime(json.load(fh)["test_start"], "%Y-%m-%dT%H:%M:%S")
    rows = load_rows(args.clean, start).iloc[:args.limit]
    print("loaded %d rows from %s" % (len(rows), args.clean), file=sys.stderr)
    result = run(r or bus.connect(), rows, args.repeat, args.stream, args.maxlen)
    print("median: %.0f msg/s over %d runs of %d messages (Redis at %s)" % (
        result["median_msg_per_s"], result["runs"], result["messages"], result["host"]))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, indent=1)
    return result


if __name__ == "__main__":
    main()
