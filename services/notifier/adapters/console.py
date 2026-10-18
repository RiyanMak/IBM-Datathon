"""Console adapter: cards as text on stdout, clicks typed on stdin.

The no-network demo path and what the tests use:

    freeze R-0042
    dismiss R-0042 looks like payroll
    more_info R-0042 ask the bank for KYC documents

Lines are read in a background thread from `lines` (stdin by default), so tests can
pass a list instead.
"""
import getpass
import sys
import threading

from services.notifier.adapters.base import Adapter

ALIASES = {
    "freeze": "freeze",
    "dismiss": "dismiss",
    "more_info": "more_info",
    "more-info": "more_info",
    "more": "more_info",
}

WIDTH = 72


def render(card):
    """A card as plain text."""
    out = ["=" * WIDTH, card["title"] + "  (v%s)" % card["version"]]
    for marker in card["markers"]:
        out.append(">> " + marker)
    for note in card["notes"]:
        out.append("note: " + note)
    if card["decision"]:
        out.append("decision: " + card["decision"]["text"])
        if card["decision"]["note"]:
            out.append("  note: " + card["decision"]["note"])
    if card["fields"]:
        out.append("")
        for label, value in card["fields"]:
            out.append("  %-16s %s" % (label + ":", value))
    for _key, label, text in card["parts"]:
        out.append("")
        out.append(label + ":")
        out.append("  " + text)
    out.append("")
    if card["buttons"]:
        out.append("type: " + " | ".join("%s %s" % (b["decision"], card["ring_id"]) for b in card["buttons"])
                   + "   (a note may follow)")
    for line in card["footer"]:
        out.append("  " + line)
    out.append("  Dashboard: " + card["dashboard_url"])
    return "\n".join(out)


class ConsoleAdapter(Adapter):
    name = "console"
    can_update = True
    can_upload = True

    def __init__(self, out=None, lines=None, user=None):
        self.out = out or sys.stdout
        self.lines = lines
        self.user = user or _user()
        self.callback = None
        self.thread = None
        self.posted = 0
        self._lock = threading.Lock()

    def say(self, text):
        with self._lock:
            print(text, file=self.out, flush=True)

    def post(self, card):
        self.posted += 1
        self.say(render(card))
        return {"console": self.posted}

    def update(self, ref, card):
        self.say("(update to message %s)\n%s" % (ref.get("console", "?"), render(card)))

    def upload(self, ref, filename, text):
        self.say("(case packet %s ready: %d lines; print it with --packet %s)" % (
            filename, text.count("\n") + 1, filename.split("_")[1] if "_" in filename else ""))

    def on_click(self, callback):
        self.callback = callback
        source = self.lines if self.lines is not None else sys.stdin
        self.thread = threading.Thread(target=self._read, args=(source,), daemon=True)
        self.thread.start()

    def _read(self, source):
        try:
            for line in source:
                self.handle_line(line)
        except (OSError, ValueError):
            pass   # stdin closed (compose without a terminal): no clicks, the service keeps posting

    def handle_line(self, line):
        """One typed command -> one click. Returns the callback's result (or None)."""
        words = (line or "").strip().split(None, 2)
        if not words:
            return None
        decision = ALIASES.get(words[0].lower())
        if not decision or len(words) < 2:
            self.say("? type: freeze R-0042 | dismiss R-0042 note | more_info R-0042 note")
            return None
        ring_id = words[1].strip().upper()
        note = words[2] if len(words) > 2 else ""
        if self.callback is None:
            return None
        result = self.callback(ring_id, "", decision, note, "console:" + self.user, "console")
        self.say(("ok: " if result.get("ok") else "not saved: ") + result.get("message", ""))
        return result


def _user():
    try:
        return getpass.getuser()
    except Exception:
        return "investigator"
