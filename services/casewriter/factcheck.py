"""
Step 5c - the fact check (the part judges will ask about).

A Granite reply is accepted only if:
  1. it is JSON with all 5 required parts, each a piece of text;
  2. every number in it matches a number in the facts pack (rounding allowed:
     10,432 may appear as 10,400 or 10k; 1.8 may appear as 2);
  3. every account ID in it belongs to the ring;
  4. every date/time in it falls inside the ring's time window.
Any failure -> the template summary is used instead.
"""
import math
import re
from datetime import datetime

from services.casewriter.template import REQUIRED_KEYS

ACCOUNT_RE = re.compile(r"\b\d+_[0-9A-Za-z]+\b")
DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?\b")
RING_ID_RE = re.compile(r"\bR-\d+\b")
# 10,000  |  1.8  |  10k  |  2.5 M   ("$" and "%" are simply ignored)
NUMBER_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(\s?[kKmM](?![A-Za-z]))?")

# Numbers any sentence may contain without it being a "fact" ("one account").
ALWAYS_ALLOWED = {0.0, 1.0}


def numbers_in(text):
    """Every number in a piece of text, ignoring account IDs, dates and ring IDs."""
    text = ACCOUNT_RE.sub(" ", text)
    text = DATE_RE.sub(" ", text)
    text = RING_ID_RE.sub(" ", text)
    found = []
    for digits, suffix in NUMBER_RE.findall(text):
        digits = digits.rstrip(",")
        if not digits:
            continue
        value = float(digits.replace(",", ""))
        if suffix.strip().lower() == "k":
            value *= 1000
        elif suffix.strip().lower() == "m":
            value *= 1000000
        found.append(value)
    return found


def allowed_numbers(facts):
    """All numbers a summary may mention, taken from the facts pack."""
    allowed = set(ALWAYS_ALLOWED)
    for name, value in facts.items():
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            allowed.add(float(value))
        elif isinstance(value, str) and name not in ("first_seen", "last_seen"):
            allowed.update(numbers_in(value))
        elif isinstance(value, list):
            for item in value:
                allowed.update(numbers_in(str(item)))
    # Scores may be written as percentages (0.93 -> 93%).
    for name in ("xgb_score", "gnn_score"):
        if facts.get(name) is not None:
            allowed.add(round(facts[name] * 100, 2))
    # Hours may be written in minutes (1.8 hours -> 108 minutes).
    for name in ("hours_to_pass_on", "duration_hours"):
        if facts.get(name) is not None:
            allowed.add(round(facts[name] * 60, 2))
    # Parts of the dates (a reply may write "September 8, 2022 at 14:00").
    for name in ("first_seen", "last_seen"):
        if facts.get(name):
            dt = datetime.strptime(facts[name], "%Y-%m-%dT%H:%M:%S")
            allowed.update(float(x) for x in (dt.year, dt.month, dt.day, dt.hour, dt.minute))
    return allowed


def is_close(n, v):
    """True if n is v, or v rounded in a normal human way."""
    candidates = {round(v), round(v, 1), round(v, 2)}
    if v != 0:
        magnitude = math.floor(math.log10(abs(v)))
        for significant in (2, 3):
            candidates.add(round(v, significant - 1 - magnitude))
    return any(abs(n - c) < 1e-6 for c in candidates)


def check_reply(reply, facts, ring_accounts):
    """Return (True, "ok") or (False, "reason it failed")."""
    if not isinstance(reply, dict):
        return False, "reply is not a JSON object"
    for key in REQUIRED_KEYS:
        if not isinstance(reply.get(key), str) or not reply[key].strip():
            return False, "missing or empty part: %s" % key

    text = " ".join(reply[key] for key in REQUIRED_KEYS)

    accounts = set(ring_accounts)
    for acct in ACCOUNT_RE.findall(text):
        if acct not in accounts:
            return False, "account not in ring: %s" % acct

    start = datetime.strptime(facts["first_seen"], "%Y-%m-%dT%H:%M:%S")
    end = datetime.strptime(facts["last_seen"], "%Y-%m-%dT%H:%M:%S")
    for y, mo, d, h, mi, _s in DATE_RE.findall(text):
        day = datetime(int(y), int(mo), int(d))
        if not (start.date() <= day.date() <= end.date()):
            return False, "date outside the ring: %s-%s-%s" % (y, mo, d)
        if h:
            moment = datetime(int(y), int(mo), int(d), int(h), int(mi))
            if not (start.replace(second=0) <= moment <= end):
                return False, "time outside the ring: %s-%s-%s %s:%s" % (y, mo, d, h, mi)

    allowed = allowed_numbers(facts)
    for n in numbers_in(text):
        if not any(is_close(n, v) for v in allowed):
            return False, "number not in facts: %g" % n

    return True, "ok"
