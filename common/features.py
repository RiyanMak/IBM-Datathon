"""Behaviour clues (features) for one bank transfer, computed from the past only.

One class, used in two places so the clues can never drift apart:
  * offline: loop over the whole clean table in time order to build features.csv
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
SIX_HOURS = 6 * HOUR      # IBM's scatter-gather window
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
    # added later, at the end so the first 32 keep their positions (older models select by name)
    "back_and_forth_24h", "snd_payees_6h", "rcv_payers_6h", "snd_out_per_payee_24h",
    "snd_amt_std_ratio", "rcv_amt_std_ratio", "snd_gap_mean_min",
]


def to_minutes(ts) -> int:
    """ISO text such as '2022-09-01T00:20:00', a pandas Timestamp or a datetime
    (as common.streams.message_to_txn returns) -> whole minutes since epoch."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if hasattr(ts, "value"):    # pandas Timestamp
        return int(ts.value // 60_000_000_000)
    return int(ts.replace(tzinfo=timezone.utc).timestamp() // 60)


def _spread(n: int, total: int, total_sq: int) -> float:
    """Standard deviation divided by the mean of n amounts, from their integer sums.

    std / mean = sqrt(n * sum_sq - sum ** 2) / sum; 0 when there is nothing to spread
    (no amounts, a single amount or a zero total).
    """
    if n < 2 or total <= 0:
        return 0.0
    return math.sqrt(n * total_sq - total * total) / total


def _bank(acct: str) -> str:
    """Account IDs are '<bank>_<account>', e.g. '10_8000EBD30'."""
    return acct.split("_", 1)[0]


class _Account:
    __slots__ = ("inbound", "outbound", "in_cents", "out_cents", "payees",
                 "first_seen", "last_in", "last_out", "first_out", "n_in", "sum_in", "n_out", "sum_out",
                 "life_in_cents", "life_in_sq", "life_out_cents", "life_out_sq")

    def __init__(self, now: int):
        self.inbound = deque()      # (ts_min, cents, other_acct), last 24 h
        self.outbound = deque()
        # Running sums of the two deques, in whole cents. Integers stay exact when
        # old transfers are subtracted; floats left residues like -2.8e-17 behind,
        # and a residue divided by a residue gave pass-through values such as -25.
        self.in_cents = 0
        self.out_cents = 0
        self.payees = set()         # everyone this account has ever paid
        self.first_seen = now
        self.last_in = None
        self.last_out = None
        self.first_out = None       # with last_out and n_out gives the mean gap between payments
        self.n_in = 0               # lifetime counts and totals, for averages (only ever added to)
        self.sum_in = 0.0
        self.n_out = 0
        self.sum_out = 0.0
        # Lifetime sums of amounts and squared amounts, in whole cents, for the spread of
        # amounts. Python integers never overflow or round, so n * sum_sq - sum ** 2 is exact
        # and never slightly negative (a float version cancels catastrophically).
        self.life_in_cents = 0
        self.life_in_sq = 0
        self.life_out_cents = 0
        self.life_out_sq = 0

    @property
    def in_sum(self) -> float:
        return self.in_cents / 100.0

    @property
    def out_sum(self) -> float:
        return self.out_cents / 100.0

    def prune(self, now: int) -> None:
        cutoff = now - DAY
        while self.inbound and self.inbound[0][0] < cutoff:
            self.in_cents -= self.inbound.popleft()[1]
        while self.outbound and self.outbound[0][0] < cutoff:
            self.out_cents -= self.outbound.popleft()[1]

    def add_in(self, ts: int, amount: float, other: str) -> None:
        cents = round(amount * 100)
        self.inbound.append((ts, cents, other))
        self.in_cents += cents
        if len(self.inbound) > MAX_DEQUE:
            self.in_cents -= self.inbound.popleft()[1]
        self.last_in = ts
        self.n_in += 1
        self.sum_in += amount
        self.life_in_cents += cents
        self.life_in_sq += cents * cents

    def add_out(self, ts: int, amount: float, other: str) -> None:
        cents = round(amount * 100)
        self.outbound.append((ts, cents, other))
        self.out_cents += cents
        if len(self.outbound) > MAX_DEQUE:
            self.out_cents -= self.outbound.popleft()[1]
        self.payees.add(other)
        if self.first_out is None:
            self.first_out = ts
        self.last_out = ts
        self.n_out += 1
        self.sum_out += amount
        self.life_out_cents += cents
        self.life_out_sq += cents * cents


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
        # one pass over the sender's outbound window for its 24 h payees, 6 h payees and 1 h count
        payees_24h, payees_6h, out_1h = set(), set(), 0
        for t, _, o in snd.outbound:
            payees_24h.add(o)
            if t > ts_min - SIX_HOURS:
                payees_6h.add(o)
                if t > ts_min - HOUR:
                    out_1h += 1
        row.append(float(len(payees_24h)))
        row.append(float(len(snd.inbound)))
        row.append(float(out_1h))
        row.append(amount * snd.n_out / snd.sum_out if snd.sum_out > 0 else 0.0)
        row.append((ts_min - snd.first_seen) / DAY)

        # --- the receiver's past ---
        row.append(rcv.in_sum)
        row.append(rcv.out_sum)
        row.append(min(rcv.out_sum / rcv.in_sum, PASS_THROUGH_CAP) if rcv.in_sum > 0 else 0.0)
        row.append(float(ts_min - rcv.last_out) if rcv.last_out is not None else -1.0)
        payers_24h, payers_6h = set(), set()
        for t, _, o in rcv.inbound:
            payers_24h.add(o)
            if t > ts_min - SIX_HOURS:
                payers_6h.add(o)
        row.append(float(len(payers_24h)))
        row.append(float(len(rcv.inbound)))
        row.append(amount * rcv.n_in / rcv.sum_in if rcv.sum_in > 0 else 0.0)
        row.append((ts_min - rcv.first_seen) / DAY)

        # --- added clues (same rule: only what happened before this transfer) ---
        # times the receiver paid the sender in the last 24 h: A pays B, then B pays A back
        row.append(float(sum(1 for _, _, o in snd.inbound if o == to_acct)))
        row.append(float(len(payees_6h)))
        row.append(float(len(payers_6h)))
        # transfers per distinct payee in 24 h: many payments to few accounts, or one each to many
        row.append(len(snd.outbound) / len(payees_24h) if payees_24h else 0.0)
        row.append(_spread(snd.n_out, snd.life_out_cents, snd.life_out_sq))
        row.append(_spread(rcv.n_in, rcv.life_in_cents, rcv.life_in_sq))
        # mean minutes between the sender's payments; the gaps add up to last - first
        row.append((snd.last_out - snd.first_out) / (snd.n_out - 1) if snd.n_out >= 2 else -1.0)

        # --- only now remember the transfer ---
        snd.add_out(ts_min, amount, to_acct)
        rcv.add_in(ts_min, amount, from_acct)
        return row
