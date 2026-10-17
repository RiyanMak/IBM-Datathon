"""The message format on the `txns` stream (replayer -> scorer).

The replayer writes one Redis stream message per transaction. The scorer
reads them back. Both sides use the two functions in this file, so the format
is defined in exactly one place:

    txn_to_message(row, sent_at)  ->  {field: text}     (replayer, before XADD)
    message_to_txn(fields)        ->  {field: value}    (scorer, after XREAD)

Every message has exactly these fields (Redis stores everything as text):

    txn_id      plain integer, e.g. "4062463"
    ts          data time "YYYY-MM-DDTHH:MM:SS" (no timezone, no "Z")
    from_acct   sender account id, e.g. "70_100428660"
    to_acct     receiver account id
    amount_usd  amount in US dollars
    amt_paid    amount paid, in ccy_paid
    ccy_paid    currency name, e.g. "US Dollar"
    amt_recv    amount received, in ccy_recv
    ccy_recv    currency name
    fmt         payment format, e.g. "Cheque"
    sent_at     wall-clock time the replayer sent it, Unix seconds with
                milliseconds, e.g. "1792345678.123". The scorer can compute
                latency as (its own now_sent_at()) - sent_at.

Amounts are written with repr(float(x)), the shortest text that turns back
into exactly the same float. They are never rounded.

There is deliberately NO label, split or source_row field: the scorer must
not see the answer.

This module only uses the standard library, so it can be imported anywhere.
"""

import math
import time
from datetime import datetime

TS_FORMAT_ISO = "%Y-%m-%dT%H:%M:%S"

# The fields of a txns message, in the order the replayer writes them.
TXN_FIELDS = (
    "txn_id",
    "ts",
    "from_acct",
    "to_acct",
    "amount_usd",
    "amt_paid",
    "ccy_paid",
    "amt_recv",
    "ccy_recv",
    "fmt",
    "sent_at",
)

# The columns the replayer needs from clean.csv (everything except sent_at).
TXN_COLUMNS = TXN_FIELDS[:-1]

AMOUNT_FIELDS = ("amount_usd", "amt_paid", "amt_recv")
TEXT_FIELDS = ("from_acct", "to_acct", "ccy_paid", "ccy_recv", "fmt")


def format_epoch(t: float) -> str:
    """Unix seconds as text with millisecond precision, e.g. "1792345678.123"."""
    return f"{t:.3f}"


def now_sent_at() -> str:
    """The current wall-clock time in the sent_at format."""
    return format_epoch(time.time())


def _is_missing(value) -> bool:
    # None, float NaN, numpy NaN, pandas NaT / NA all count as missing.
    if value is None:
        return True
    try:
        return bool(value != value)   # NaN is the only value not equal to itself
    except (TypeError, ValueError):
        return True                   # pandas.NA raises on bool()


def _ts_text(ts) -> str:
    """Timestamp (datetime, pandas Timestamp or ISO text) -> "YYYY-MM-DDTHH:MM:SS"."""
    if isinstance(ts, str):
        datetime.strptime(ts, TS_FORMAT_ISO)   # raises ValueError if the format is wrong
        return ts
    if getattr(ts, "tzinfo", None) is not None:
        raise ValueError(f"ts must not have a timezone, got {ts!r}")
    if getattr(ts, "microsecond", 0) or getattr(ts, "nanosecond", 0):
        raise ValueError(f"ts has fractions of a second, which the format cannot hold: {ts!r}")
    return ts.strftime(TS_FORMAT_ISO)


def txn_to_message(row: dict, sent_at: str) -> dict:
    """One transaction (a dict with the TXN_COLUMNS keys) -> stream fields (all text).

    Extra keys in row (for example label) are ignored, so they can never leak
    into the message. Raises ValueError if a value is missing.
    """
    msg = {}
    for name in TXN_COLUMNS:
        if name not in row:
            raise ValueError(f"row is missing the column {name!r}")
        value = row[name]
        if _is_missing(value):
            raise ValueError(f"{name} is missing (txn_id {row.get('txn_id')})")
        if name == "txn_id":
            msg[name] = str(int(value))
        elif name == "ts":
            msg[name] = _ts_text(value)
        elif name in AMOUNT_FIELDS:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{name} is not a finite number: {value!r}")
            msg[name] = repr(number)   # shortest exact text; float(text) == number
        else:
            msg[name] = str(value)
    msg["sent_at"] = str(sent_at)
    return msg


def message_to_txn(fields: dict) -> dict:
    """Stream fields (text) -> Python values.

    txn_id -> int, ts -> datetime (no timezone), amounts -> float,
    sent_at -> float, everything else stays text.
    Raises ValueError if a field is missing or an unexpected field is present.
    """
    missing = [f for f in TXN_FIELDS if f not in fields]
    extra = sorted(set(fields) - set(TXN_FIELDS))
    if missing or extra:
        raise ValueError(f"bad txns message: missing {missing}, unexpected {extra}")

    txn = {}
    for name in TXN_FIELDS:
        text = fields[name]
        if name == "txn_id":
            txn[name] = int(text)
        elif name == "ts":
            txn[name] = datetime.strptime(text, TS_FORMAT_ISO)
        elif name in AMOUNT_FIELDS or name == "sent_at":
            txn[name] = float(text)
        else:
            txn[name] = str(text)
    return txn
