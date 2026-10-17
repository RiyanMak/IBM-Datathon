"""Behaviour clues (features) for one bank transfer, computed from the past only.

One class, used in two places so the clues can never drift apart:
  * offline: loop over the whole clean table in time order to build features.parquet
  * live:    inside the scorer, on each message from the txns stream

The rule that makes this safe: compute the clues for a transfer FIRST, then add
the transfer to the accounts' memory. Every clue therefore only sees transfers
that arrived earlier.
"""

import math
from collections import deque
from datetime import datetime, timezone

HOUR = 60                 # all times are whole minutes since epoch
DAY = 24 * HOUR
MAX_DEQUE = 2000          # cap per-account memory so one bank-like hub can't slow everything
PASS_THROUGH_CAP = 5.0

FORMATS = ["ACH", "Bitcoin", "Cash", "Cheque", "Credit Card", "Reinvestment", "Wire"]

# The exact order the model expects. models/feature_list.json is written from this.
FEATURE_NAMES = [
    # the transfer itself
    "log_amount", "is_round",
    *[f"fmt_{f.replace(' ', '_')}" for f in FORMATS],
    "cross_currency", "cross_bank", "self_transfer", "hour", "weekday",
    # the sender's past
    "snd_in_sum_24h", "snd_out_sum_24h", "pass_through", "mins_since_snd_last_in",
    "new_payee", "snd_payees_24h", "snd_in_count_24h", "snd_out_count_1h",
    "amt_vs_snd_avg", "snd_age_days",
    # the receiver's past
    "rcv_in_sum_24h", "rcv_out_sum_24h", "rcv_pass_through", "mins_since_rcv_last_out",
    "rcv_payers_24h", "rcv_in_count_24h", "amt_vs_rcv_avg", "rcv_age_days",
]


def to_minutes(ts) -> int:
    """ISO text such as '2022-09-01T00:20:00' (or a pandas Timestamp) -> whole minutes since epoch."""
    if isinstance(ts, str):
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() // 60)
    return int(ts.value // 60_000_000_000)


def _bank(acct: str) -> str:
    """Account IDs are '<bank>_<account>', e.g. '10_8000EBD30'."""
    return acct.split("_", 1)[0]


class _Account:
    __slots__ = ("inbound", "outbound", "in_sum", "out_sum", "payees",
                 "first_seen", "last_in", "last_out", "n_in", "sum_in", "n_out", "sum_out")

    def __init__(self, now: int):
        self.inbound = deque()      # (ts_min, amount_usd, other_acct), last 24 h
        self.outbound = deque()
        self.in_sum = 0.0           # running sums of the two deques
        self.out_sum = 0.0
        self.payees = set()         # everyone this account has ever paid
        self.first_seen = now
        self.last_in = None
        self.last_out = None
        self.n_in = 0               # lifetime counts and totals, for averages
        self.sum_in = 0.0
        self.n_out = 0
        self.sum_out = 0.0

    def prune(self, now: int) -> None:
        cutoff = now - DAY
        while self.inbound and self.inbound[0][0] < cutoff:
            self.in_sum -= self.inbound.popleft()[1]
        while self.outbound and self.outbound[0][0] < cutoff:
            self.out_sum -= self.outbound.popleft()[1]

    def add_in(self, ts: int, amount: float, other: str) -> None:
        self.inbound.append((ts, amount, other))
        self.in_sum += amount
        if len(self.inbound) > MAX_DEQUE:
            self.in_sum -= self.inbound.popleft()[1]
        self.last_in = ts
        self.n_in += 1
        self.sum_in += amount

    def add_out(self, ts: int, amount: float, other: str) -> None:
        self.outbound.append((ts, amount, other))
        self.out_sum += amount
        if len(self.outbound) > MAX_DEQUE:
            self.out_sum -= self.outbound.popleft()[1]
        self.payees.add(other)
        self.last_out = ts
        self.n_out += 1
        self.sum_out += amount


class ClueBuilder:
    """Feed transfers in time order; each call returns the clues for that transfer."""

    def __init__(self):
        self._accts = {}

    def compute(self, txn: dict) -> dict:
        """Live path. txn has the txns-stream fields (all text). Returns {clue name: value}."""
        row = self.compute_row(
            to_minutes(txn["ts"]), txn["from_acct"], txn["to_acct"],
            float(txn["amount_usd"]), txn["ccy_paid"], txn["ccy_recv"], txn["fmt"],
        )
        return dict(zip(FEATURE_NAMES, row))

    def compute_row(self, ts_min, from_acct, to_acct, amount_usd, ccy_paid, ccy_recv, fmt):
        """Offline path. Returns the clue values as a list in FEATURE_NAMES order."""
        accts = self._accts
        snd = accts.get(from_acct)
        if snd is None:
            snd = accts[from_acct] = _Account(ts_min)
        rcv = accts.get(to_acct)
        if rcv is None:
            rcv = accts[to_acct] = _Account(ts_min)
        snd.prune(ts_min)
        rcv.prune(ts_min)
        amount = float(amount_usd)

        # --- the transfer itself ---
        row = [
            math.log10(1.0 + max(amount, 0.0)),
            1.0 if amount > 0 and abs(amount / 100 - round(amount / 100)) < 1e-6 else 0.0,
        ]
        row.extend(1.0 if fmt == f else 0.0 for f in FORMATS)
        row.append(1.0 if ccy_paid != ccy_recv else 0.0)
        row.append(1.0 if _bank(from_acct) != _bank(to_acct) else 0.0)
        row.append(1.0 if from_acct == to_acct else 0.0)
        row.append(float((ts_min // HOUR) % 24))
        row.append(float((ts_min // DAY + 3) % 7))   # 1970-01-01 was a Thursday; Monday = 0

        # --- the sender's past ---
        row.append(snd.in_sum)
        row.append(snd.out_sum)
        row.append(min(snd.out_sum / snd.in_sum, PASS_THROUGH_CAP) if snd.in_sum > 0 else 0.0)
        row.append(float(ts_min - snd.last_in) if snd.last_in is not None else -1.0)
        row.append(0.0 if to_acct in snd.payees else 1.0)
        row.append(float(len({o for _, _, o in snd.outbound})))
        row.append(float(len(snd.inbound)))
        row.append(float(sum(1 for t, _, _ in snd.outbound if t > ts_min - HOUR)))
        row.append(amount * snd.n_out / snd.sum_out if snd.sum_out > 0 else 0.0)
        row.append((ts_min - snd.first_seen) / DAY)

        # --- the receiver's past ---
        row.append(rcv.in_sum)
        row.append(rcv.out_sum)
        row.append(min(rcv.out_sum / rcv.in_sum, PASS_THROUGH_CAP) if rcv.in_sum > 0 else 0.0)
        row.append(float(ts_min - rcv.last_out) if rcv.last_out is not None else -1.0)
        row.append(float(len({o for _, _, o in rcv.inbound})))
        row.append(float(len(rcv.inbound)))
        row.append(amount * rcv.n_in / rcv.sum_in if rcv.sum_in > 0 else 0.0)
        row.append((ts_min - rcv.first_seen) / DAY)

        # --- only now remember the transfer ---
        snd.add_out(ts_min, amount, to_acct)
        rcv.add_in(ts_min, amount, from_acct)
        return row
