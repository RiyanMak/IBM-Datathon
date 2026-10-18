"""Turn the clues that pushed a score up into short phrases an investigator can read.

For each transfer at or above the ring threshold the scorer computes SHAP
contributions (one number per clue) and keeps the three largest positive ones.
This table maps each clue name to a phrase; the clue's value is filled in.

The wording is Pair 1's: edit the templates here and nothing else changes. The
same phrases go to the investigator page and into Granite's facts.
"""

from common.features import FEATURE_NAMES, FORMATS

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _minutes(v: float) -> str:
    v = int(round(v))
    if v < 120:
        return f"{v} minutes"
    if v < 48 * 60:
        return f"{v // 60} hours"
    return f"{v // 1440} days"


def _days(v: float) -> str:
    return "first seen today" if v < 1 else f"{v:.0f} days old"


def _pct(v: float) -> str:
    """Pass-through ratio (out / in, capped at 5)."""
    return "more than 100%" if v > 1.0 else f"{v:.0%}"


# clue name -> function(value, amount_usd) -> phrase
TEMPLATES = {
    "log_amount": lambda v, amt: f"unusually large: {_money(amt)}",
    "is_round": lambda v, amt: f"a round amount of {_money(amt)}",
    "cross_currency": lambda v, amt: "changed currency on the way",
    "cross_bank": lambda v, amt: "sent to an account at another bank",
    "self_transfer": lambda v, amt: "sent to the same account",
    "hour": lambda v, amt: f"sent at {int(v):02d}:00",
    "weekday": lambda v, amt: f"sent on a {DAYS[int(v) % 7]}",
    # the sender's past
    "snd_in_sum_24h": lambda v, amt: f"sender received {_money(v)} in the last 24 hours",
    "snd_out_sum_24h": lambda v, amt: f"sender paid out {_money(v)} in the last 24 hours",
    "pass_through": lambda v, amt: f"{_pct(v)} of money received was passed straight on",
    "mins_since_snd_last_in": lambda v, amt: (
        "sender has never received money" if v < 0
        else "money left within a minute of arriving" if v < 1
        else f"money left {_minutes(v)} after it arrived"),
    "new_payee": lambda v, amt: "paid someone this account had never paid before",
    "snd_payees_24h": lambda v, amt: f"paid {int(v)} different accounts in 24 hours",
    "snd_in_count_24h": lambda v, amt: f"sender received {int(v)} transfers in 24 hours",
    "snd_out_count_1h": lambda v, amt: f"{int(v)} transfers sent in the last hour",
    "amt_vs_snd_avg": lambda v, amt: f"{v:.1f} times the sender's usual amount",
    "snd_age_days": lambda v, amt: f"sender account {_days(v)}",
    # the receiver's past
    "rcv_in_sum_24h": lambda v, amt: f"receiver took in {_money(v)} in the last 24 hours",
    "rcv_out_sum_24h": lambda v, amt: f"receiver paid out {_money(v)} in the last 24 hours",
    "rcv_pass_through": lambda v, amt: f"receiver passed on {_pct(v)} of what it received",
    "mins_since_rcv_last_out": lambda v, amt: (
        "receiver has never sent money" if v < 0
        else f"receiver last sent money {_minutes(v)} ago"),
    "rcv_payers_24h": lambda v, amt: f"receiver was paid by {int(v)} different accounts in 24 hours",
    "rcv_in_count_24h": lambda v, amt: f"receiver got {int(v)} transfers in 24 hours",
    "amt_vs_rcv_avg": lambda v, amt: f"{v:.1f} times the receiver's usual amount",
    "rcv_age_days": lambda v, amt: f"receiver account {_days(v)}",
}
for _fmt in FORMATS:
    TEMPLATES[f"fmt_{_fmt.replace(' ', '_')}"] = (lambda f: lambda v, amt: f"paid by {f}")(_fmt)

# 0/1 clues: a phrase only makes sense when the clue is 1 ("paid by Wire", not "not paid by Wire")
BINARY = {"is_round", "cross_currency", "cross_bank", "self_transfer", "new_payee",
          *[f"fmt_{f.replace(' ', '_')}" for f in FORMATS]}

assert set(TEMPLATES) == set(FEATURE_NAMES), "every clue needs a phrase"


def phrases(values, contribs, amount_usd: float, k: int = 3, names=FEATURE_NAMES) -> list:
    """The k clues with the largest positive SHAP contribution, as phrases.

    values and contribs are aligned with names. Clues with a contribution of 0
    or less never appear; binary clues that are 0 are skipped.
    """
    order = sorted(range(len(names)), key=lambda i: -float(contribs[i]))
    out = []
    for i in order:
        if len(out) == k or float(contribs[i]) <= 0:
            break
        name, v = names[i], float(values[i])
        if name in BINARY and v < 0.5:
            continue
        out.append(TEMPLATES[name](v, float(amount_usd)))
    return out
