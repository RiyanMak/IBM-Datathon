"""
Step 5a - turn one ring message into a "facts pack".

A ring arrives from the ring service (step 4) on the Redis stream `rings`.
This file reads it and computes every number the case summary is allowed
to mention. Granite never computes numbers; it only gets these facts.
"""
import json
from collections import defaultdict
from datetime import datetime

# The order of fields when an edge is sent as a list instead of a dictionary.
# Agreed in "Handoffs between steps": txn_id, from, to, amount_usd, ts, score
EDGE_FIELDS = ["txn_id", "from", "to", "amount_usd", "ts", "score"]

# An account only counts as the "collector" (or the "origin") if it ends up
# holding (or giving away) at least this share of all money in the ring.
MAIN_ACCOUNT_SHARE = 0.25

# Transfers between these two amounts count as "just under the reporting
# threshold" (a sign of structuring: keeping each payment small to avoid reports).
REPORTING_THRESHOLD_USD = 10000
JUST_UNDER_LOW = 9000


def _text(value):
    """Redis can hand us bytes; turn them into normal text."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return value


def _json_field(value, default):
    """Lists and dictionaries travel as JSON text. Turn them back into Python."""
    value = _text(value)
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def _number(value):
    value = _text(value)
    if value is None or value == "" or value == "None":
        return None
    return float(value)


def parse_time(value):
    """Accept the ISO format we agreed on, plus the dataset's original format."""
    text = str(_text(value)).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError("Unrecognised time: %r" % (value,))


def parse_edge(edge):
    """One transfer inside a ring. Works whether it was sent as a dict or a list."""
    if isinstance(edge, dict):
        e = {k: edge.get(k) for k in EDGE_FIELDS}
        if e["from"] is None:
            e["from"] = edge.get("from_acct")
        if e["to"] is None:
            e["to"] = edge.get("to_acct")
    elif isinstance(edge, (list, tuple)) and len(edge) >= 5:
        padded = list(edge) + [None] * (len(EDGE_FIELDS) - len(edge))
        e = dict(zip(EDGE_FIELDS, padded))
    else:
        raise ValueError("Unrecognised edge: %r" % (edge,))
    return {
        "txn_id": str(e["txn_id"]),
        "from": str(e["from"]),
        "to": str(e["to"]),
        "amount_usd": float(e["amount_usd"]),
        "ts": parse_time(e["ts"]),
        "score": _number(e["score"]),
    }


def parse_ring(fields):
    """Read one message from the `rings` stream (all values arrive as text)."""
    f = {_text(k): _text(v) for k, v in fields.items()}
    edges = [parse_edge(e) for e in _json_field(f.get("edges"), [])]
    if not edges:
        raise ValueError("ring %s has no edges" % f.get("ring_id"))
    accounts = [str(a) for a in _json_field(f.get("accounts"), [])]
    if not accounts:
        accounts = sorted({e["from"] for e in edges} | {e["to"] for e in edges})
    return {
        "ring_id": str(f.get("ring_id") or "R-unknown"),
        "version": str(f.get("version") or "1"),
        "shape": str(f.get("shape") or "unknown"),
        "accounts": accounts,
        "edges": edges,
        "xgb_score": _number(f.get("xgb_score")),
        "gnn_score": _number(f.get("gnn_score")),
        "reasons": [str(r) for r in _json_field(f.get("reasons"), [])],
    }


def normalise_shape(shape):
    """'SCATTER-GATHER', 'scatter_gather', 'Simple Cycle' -> 'scatter-gather', 'cycle'."""
    s = str(shape).strip().lower().replace("_", "-").replace(" ", "-")
    s = s.split(":")[0]
    if s in ("simple-cycle", "cycles"):
        s = "cycle"
    return s


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def _round_or_none(value, digits):
    return None if value is None else round(value, digits)


def build_facts(ring):
    """Compute the facts pack. Every number in a summary must come from here."""
    edges = sorted(ring["edges"], key=lambda e: e["ts"])
    money_in = defaultdict(float)
    money_out = defaultdict(float)
    for e in edges:
        money_out[e["from"]] += e["amount_usd"]
        money_in[e["to"]] += e["amount_usd"]

    accounts = set(ring["accounts"]) | set(money_in) | set(money_out)
    total = sum(e["amount_usd"] for e in edges)

    # Key account = the account that passed the most money straight through
    # (it both received and sent on). That is the likely recruited mule.
    passed_through = {a: min(money_in[a], money_out[a]) for a in accounts}
    key = max(sorted(accounts), key=lambda a: (passed_through[a], money_out[a]))
    if passed_through[key] == 0:
        # Nobody both received and sent (a plain fan-out or fan-in):
        # use the account that moved the most money instead.
        key = max(sorted(accounts), key=lambda a: max(money_out[a], money_in[a]))

    received = money_in[key]
    sent = money_out[key]
    first_in = min((e["ts"] for e in edges if e["to"] == key), default=None)
    first_out = None
    if first_in is not None:
        first_out = min((e["ts"] for e in edges
                         if e["from"] == key and e["ts"] >= first_in), default=None)
    hours_to_pass_on = None
    if first_in is not None and first_out is not None:
        hours_to_pass_on = (first_out - first_in).total_seconds() / 3600

    # Collector = where the money ends up; origin = where it comes from.
    net = {a: money_in[a] - money_out[a] for a in accounts}
    collector = max(sorted(accounts), key=lambda a: net[a])
    collector_senders = len({e["from"] for e in edges if e["to"] == collector})
    if (collector == key or net[collector] < MAIN_ACCOUNT_SHARE * total
            or collector_senders < 2):   # a collector gathers from several accounts
        collector = None
    origin = min(sorted(accounts), key=lambda a: net[a])
    if origin == key or -net[origin] < MAIN_ACCOUNT_SHARE * total:
        origin = None

    amounts = [e["amount_usd"] for e in edges]
    just_under = [a for a in amounts
                  if JUST_UNDER_LOW <= a < REPORTING_THRESHOLD_USD]

    first_seen, last_seen = edges[0]["ts"], edges[-1]["ts"]
    edge_scores = [e["score"] for e in edges if e["score"] is not None]
    xgb = ring["xgb_score"]
    if xgb is None and edge_scores:
        xgb = sum(edge_scores) / len(edge_scores)

    return {
        "ring_id": ring["ring_id"],
        "shape": normalise_shape(ring["shape"]),
        "n_accounts": len(accounts),
        "n_transfers": len(edges),
        "total_usd": round(total),
        "first_seen": _iso(first_seen),
        "last_seen": _iso(last_seen),
        "duration_hours": round((last_seen - first_seen).total_seconds() / 3600, 1),
        "key_account": key,
        "key_received_usd": round(received),
        "key_sent_usd": round(sent),
        "passed_on_pct": round(100 * sent / received) if received > 0 else None,
        "hours_to_pass_on": _round_or_none(hours_to_pass_on, 1),
        "key_payees": len({e["to"] for e in edges if e["from"] == key}),
        "key_senders": len({e["from"] for e in edges if e["to"] == key}),
        "origin": origin,
        "origin_sent_usd": round(money_out[origin]) if origin else None,
        "collector": collector,
        "collector_received_usd": round(money_in[collector]) if collector else None,
        "min_transfer_usd": round(min(amounts)),
        "max_transfer_usd": round(max(amounts)),
        "n_just_under_threshold": len(just_under),
        "reporting_threshold_usd": REPORTING_THRESHOLD_USD,
        "xgb_score": _round_or_none(xgb, 2),
        "gnn_score": _round_or_none(ring["gnn_score"], 2),
        "top_reasons": ring["reasons"][:3],
    }
