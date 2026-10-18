"""Entry point used by docker-compose: python -m services.notifier.main

    python -m services.notifier.main                    the service (adapter from NOTIFIER_ADAPTER)
    python -m services.notifier.main --console          print cards, take decisions typed on stdin
    python -m services.notifier.main --console --once   handle what is on `cases`, then exit
    python -m services.notifier.main --packet R-0042    print one ring's case packet and exit

Redis host and stream names come from common/bus.py (REDIS_HOST, default "redis").
"""
import argparse
import os
import socket
import sys

from common import bus
from services.notifier import adapters, cards, decisions, packet
from services.notifier.notifier import Notifier, RingCache, find_case


def load_env_file(path=".env"):
    """Read KEY=value lines from .env (if it exists) without extra libraries.
    Values already in the environment win, as with docker compose's env_file."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def print_packet(r, ring_id, out):
    case = find_case(r, ring_id)
    if case is None:
        print("no case for %s on '%s'" % (ring_id, bus.CASES), file=sys.stderr)
        return 1
    rings = RingCache(r)
    rings.refresh()
    print(packet.render(case, decisions.history(r, ring_id), rings.get(ring_id, case["version"])), file=out)
    return 0


def main(argv=None, r=None, out=None, lines=None):
    """argv, r (a Redis client), out and lines (stdin replacement) are for tests."""
    load_env_file()
    p = argparse.ArgumentParser(description="Mule Ring Hunter reviewer bot (notifier)")
    p.add_argument("--console", action="store_true", help="use the console adapter whatever NOTIFIER_ADAPTER says")
    p.add_argument("--packet", metavar="RING_ID", help="print the case packet for one ring and exit")
    p.add_argument("--once", action="store_true", help="handle what is waiting on `cases`, then exit")
    p.add_argument("--max-loops", type=int, help="stop after this many reads (tests)")
    p.add_argument("--consumer", default=os.environ.get("NOTIFIER_NAME") or "notifier",
                   help="name in the consumer group (run one notifier; a fixed name keeps its pending list "
                        "across container re-creation)")
    args = p.parse_args(argv)
    out = out or sys.stdout
    r = r or bus.connect()

    if args.packet:
        return print_packet(r, args.packet.strip().upper(), out)

    name = "console" if args.console else adapters.choose()
    try:
        adapter = adapters.make(name, out=out, lines=lines)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    notifier = Notifier(r, adapter, consumer=args.consumer or socket.gethostname(), dashboard=cards.dashboard_url())
    notifier.start()
    try:
        counts = notifier.run(max_loops=args.max_loops, until_idle=args.once)
    except KeyboardInterrupt:
        counts = notifier.counts
    finally:
        adapter.stop()
    print("notifier: %s" % ", ".join("%s %d" % kv for kv in counts.items()), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
