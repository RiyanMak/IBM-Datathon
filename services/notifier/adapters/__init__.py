"""Chat adapters. Only the console adapter exists for now; chat products (Slack,
Teams) are future work and would each add one module behind the same interface
(adapters/base.py)."""
import os

ADAPTERS = ("console",)


def choose(name=""):
    """The adapter name from the argument, else NOTIFIER_ADAPTER, else console."""
    return (name or os.environ.get("NOTIFIER_ADAPTER", "") or "console").strip().lower()


def make(name, **options):
    if name == "console":
        from services.notifier.adapters.console import ConsoleAdapter
        return ConsoleAdapter(**options)
    raise ValueError("unknown NOTIFIER_ADAPTER %r: only %s is available in this version"
                     % (name, ", ".join(ADAPTERS)))
