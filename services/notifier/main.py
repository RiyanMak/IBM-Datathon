"""Entry point used by docker compose: python -m services.notifier.main

    python -m services.notifier.main --console          # cards as text, clicks typed on stdin
    python -m services.notifier.main                    # adapter from NOTIFIER_ADAPTER (default console)
    python -m services.notifier.main --adapter slack    # Slack, Socket Mode (needs the three SLACK_ keys)
    python -m services.notifier.main --packet R-0042    # print the latest case packet and exit
    python -m services.notifier.main --once             # handle what is waiting, then exit (smoke test)

A chat adapter that cannot start (library not installed, token missing) falls back
to the console adapter with a clear message, so a missing network never blocks the
demo. Redis host and stream names come from common/bus.py (REDIS_HOST).
"""
import argparse
import importlib
import logging
import os
import sys

from common import bus

log = logging.getLogger("notifier")

ADAPTERS = ("console", "slack", "teams")


def load_env_file(path=".env"):
    """Read KEY=value lines from .env (if it exists) without extra libraries.
    Values already in the environment win, so docker compose's env_file is respected."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, os.environ.get(name), default)
        return float(default)


def console_adapter(why=""):
    from services.notifier.adapters.console import ConsoleAdapter
    if why:
        print("notifier: %s; using the console adapter instead" % why, file=sys.stderr)
    return ConsoleAdapter()


def slack_adapter():
    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL") if not os.environ.get(k)]
    if missing:
        return console_adapter("Slack settings missing (%s)" % ", ".join(missing))
    try:
        from services.notifier.adapters import slack
    except Exception as exc:   # slack-bolt not installed, or the adapter is not there yet
        return console_adapter("the Slack adapter could not be loaded (%s)" % exc)
    try:
        if hasattr(slack, "from_env"):
            return slack.from_env()
        return slack.SlackAdapter(bot_token=os.environ["SLACK_BOT_TOKEN"],
                                  app_token=os.environ["SLACK_APP_TOKEN"],
                                  channel=os.environ["SLACK_CHANNEL"])
    except Exception as exc:
        return console_adapter("the Slack adapter did not start (%s)" % exc)


def teams_adapter():
    if not os.environ.get("TEAMS_WEBHOOK_URL"):
        return console_adapter("TEAMS_WEBHOOK_URL is not set")
    try:
        from services.notifier.adapters import teams_webhook
        if hasattr(teams_webhook, "from_env"):
            return teams_webhook.from_env()
        return teams_webhook.TeamsWebhookAdapter(os.environ["TEAMS_WEBHOOK_URL"])
    except Exception as exc:   # notification-only Teams is not implemented yet
        return console_adapter("the Teams adapter is not available (%s)" % exc)


def make_adapter(name):
    name = (name or "console").strip().lower()
    if name == "slack":
        return slack_adapter()
    if name == "teams":
        return teams_adapter()
    if name != "console":
        print("notifier: unknown adapter %r" % name, file=sys.stderr)
    return console_adapter()


def print_packet(r, ring_id):
    """The latest case packet for one ring, on stdout. Returns the exit code."""
    try:
        packet = importlib.import_module("services.notifier.packet")
    except Exception as exc:
        print("the case packet module is not available (%s)" % exc, file=sys.stderr)
        return 2
    from services.notifier import decisions
    from services.notifier.notifier import Notifier
    n = Notifier(r, adapter=None)
    case = n._latest_case(ring_id)
    if case is None:
        print("no case for %s on '%s'" % (ring_id, bus.CASES), file=sys.stderr)
        return 1
    ring = n.rings_index().get(ring_id)
    print(packet.render(case, ring, decisions.history(r, ring_id), manifest=None))
    return 0


def main(argv=None):
    load_env_file()
    p = argparse.ArgumentParser(description="Mule Ring Hunter reviewer bot")
    p.add_argument("--console", action="store_true", help="force the console adapter")
    p.add_argument("--adapter", choices=ADAPTERS, default=os.environ.get("NOTIFIER_ADAPTER", "console") or "console",
                   help="chat adapter (default: NOTIFIER_ADAPTER, else console)")
    p.add_argument("--dashboard-url", default=os.environ.get("DASHBOARD_URL") or "http://localhost:8501")
    p.add_argument("--suppress-hours", type=float, default=env_float("SUPPRESS_HOURS", 24))
    p.add_argument("--packet", metavar="RING_ID", help="print the latest case packet for a ring and exit")
    p.add_argument("--once", action="store_true", help="handle what is waiting on Redis, then exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    r = bus.connect()
    if args.packet:
        return print_packet(r, args.packet)

    from services.notifier.notifier import Notifier
    adapter = console_adapter() if args.console else make_adapter(args.adapter)
    n = Notifier(r, adapter, args.dashboard_url, args.suppress_hours)
    if args.once:
        while n.run_once(block_ms=500):
            pass
        return 0
    n.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
