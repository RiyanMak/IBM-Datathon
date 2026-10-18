"""The console adapter: cards printed as text, clicks typed as commands. No network.

This is the demo path when there is no Slack workspace or no internet, and it is
what the tests use. Run it with:

    python -m services.notifier.main --console

Commands, one per line (the note is optional):

    freeze R-0042 accounts confirmed with the bank
    dismiss R-0042 known payroll pattern
    more_info R-0042 ask the branch for KYC documents
    quit

The person is NOTIFIER_USER from the environment, or the login name, recorded as
decided_by "console:<name>". Tests call handle_line() directly, without a thread.
"""
import getpass
import os
import sys
import threading

from services.notifier import cards
from services.notifier.adapters.base import Adapter

COMMANDS = ("freeze", "dismiss", "more_info")
RULE = "=" * 70


def user_name():
    name = os.environ.get("NOTIFIER_USER", "").strip()
    if name:
        return name
    try:
        return getpass.getuser()
    except Exception:   # no login name in some containers
        return "unknown"


def render(card, ref=""):
    """A card as readable text (also handy for logs)."""
    c = card
    lines = [RULE]
    head = "%s v%s  |  %s  |  %s accounts  |  %s" % (
        c["ring_id"], c["version"], c["shape_label"],
        cards.DASH if c["n_accounts"] is None else c["n_accounts"], cards.money(c["total_usd"]))
    lines.append(head + (("   [%s]" % ref) if ref else ""))
    lines.append("XGBoost %s  |  GNN %s" % (cards.score(c["xgb_score"]), cards.score(c["gnn_score"])))
    for m in c["markers"]:
        lines.append("  * %s" % m)
    if c["key_facts"]:
        lines.append("")
        for label, value in c["key_facts"]:
            lines.append("  %-32s %s" % (label + ":", value))
    if c["reasons"]:
        lines.append("")
        lines.append("  Signals: " + "; ".join(c["reasons"]))
    for label, text in c["summary"]:
        if text:
            lines.append("")
            lines.append("%s:" % label)
            lines.append("  " + text)
    lines.append("")
    foot = "Written by: %s" % c["source_label"]
    if c["check_note"]:
        foot += " (%s)" % c["check_note"]
    if c["written_at"]:
        foot += " at %s" % c["written_at"]
    lines.append(foot)
    if c["dashboard_url"]:
        lines.append("Dashboard: %s" % c["dashboard_url"])
    status = c["status"]
    line = "Status: %s" % status["label"]
    if status["decision"]:
        name = cards.who(status)
        line += (" by %s" % name) if name else " on the dashboard"
        if status["decided_at"]:
            line += " at %s" % cards.short_time(status["decided_at"])
    if status["note"]:
        line += " - %s" % status["note"]
    lines.append(line)
    if c["buttons_enabled"]:
        lines.append("Type: freeze %s | dismiss %s | more_info %s  [note]" % ((c["ring_id"],) * 3))
    lines.append(RULE)
    return "\n".join(lines)


class ConsoleAdapter(Adapter):
    kind = "console"

    def __init__(self, out=None, inp=None):
        self.out = out or sys.stdout
        self.inp = inp if inp is not None else sys.stdin
        self.clicks = None
        self.quit_event = threading.Event()
        self.thread = None
        self.count = 0
        self.lock = threading.Lock()

    def _print(self, text):
        with self.lock:
            print(text, file=self.out, flush=True)

    # --- sending -------------------------------------------------------------

    def post(self, card):
        self.count += 1
        ref = "console:%d" % self.count
        self._print(render(card, ref))
        return ref

    def update(self, message_ref, card):
        self._print("(update to %s)\n%s" % (message_ref, render(card, message_ref)))

    def upload(self, message_ref, filename, text):
        self._print("(attached %s to %s: %d lines)" % (filename, message_ref, len(text.splitlines())))

    # --- clicks --------------------------------------------------------------

    def start(self, clicks):
        self.clicks = clicks
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            for line in self.inp:
                self.handle_line(line)
                if self.quit_event.is_set():
                    return
        except (OSError, ValueError):   # stdin closed (e.g. under docker without -i)
            return

    def handle_line(self, line):
        """Parse one command. Returns the click dict it queued, or None."""
        parts = (line or "").strip().split(None, 2)
        if not parts:
            return None
        command = parts[0].lower()
        if command == "quit":
            self.quit_event.set()
            return None
        if command not in COMMANDS or len(parts) < 2:
            self._print("commands: freeze|dismiss|more_info <ring_id> [note], or quit")
            return None
        click = {"ring_id": parts[1], "version": "", "decision": command,
                 "user_id": "console", "user_name": user_name(),
                 "note": parts[2] if len(parts) > 2 else ""}
        if self.clicks is not None:
            self.clicks.put(click)
        return click

    def stop(self):
        self.quit_event.set()
