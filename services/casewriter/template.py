"""
Step 5b - the fill-in-the-blanks summary. This is the safety net:
every ring always gets a correct summary, even if Granite is down or wrong.

FINANCE OWNER: the wording lives in SHAPE_TEXT and in template_summary().
Edit the sentences freely, but only use numbers that come from `facts`.
Rules: say "activity consistent with", never "criminal" or "guilty".
"""

# One plain-English description per laundering shape in the dataset.
SHAPE_TEXT = {
    "fan-out": "one account split money out to many accounts",
    "fan-in": "many accounts sent money into one account",
    "scatter-gather": "money was split across several accounts and then collected again in one account",
    "gather-scatter": "money was collected from several accounts into one and then split out again",
    "cycle": "money moved through a chain of accounts and came back to where it started",
    "bipartite": "one group of accounts sent money to a second group of accounts",
    "stack": "money moved through layers of accounts, each group paying the next",
    "random": "money moved through an irregular chain of accounts",
    "other": "money moved between a connected group of accounts in no single recognised pattern",
}
UNKNOWN_SHAPE_TEXT = "money moved quickly between a connected group of accounts"

REQUIRED_KEYS = ["summary", "what_happened", "why_suspicious",
                 "possible_victim", "suggested_next_step"]

# A ring scoring at least this high gets the stronger next step.
STRONG_SCORE = 0.80


def money(x):
    return "${:,.0f}".format(x)


def when(iso_text):
    """'2022-09-08T14:00:00' -> '2022-09-08 14:00'"""
    return iso_text.replace("T", " ")[:16]


def shape_label(shape):
    """Shape name with its article, for "consistent with {shape_label} laundering pattern"."""
    if shape in SHAPE_TEXT and shape != "other":
        return "a " + shape
    return "an unusual"


def behaviour_signals(f):
    """Red flags we can see directly in the ring's transfers (no model needed).
    FINANCE OWNER: add or reword signals here. Each one must only use facts."""
    key = f["key_account"]
    signals = []
    # Pass-through: money in, money straight out (the classic mule sign).
    if (f["passed_on_pct"] is not None and f["passed_on_pct"] >= 80
            and f["hours_to_pass_on"] is not None):
        signals.append("Account {} passed on {}% of the money it received, and began sending "
                       "it on {} hours after it arrived.".format(
                           key, f["passed_on_pct"], f["hours_to_pass_on"]))
    # Many counterparties: one account collecting from or paying out to many.
    if f["key_senders"] >= 3:
        signals.append("Account {} received money from {} different accounts.".format(
            key, f["key_senders"]))
    if f["key_payees"] >= 3:
        signals.append("Account {} sent money to {} different accounts.".format(
            key, f["key_payees"]))
    # Structuring: several payments kept just under the reporting threshold.
    if f["n_just_under_threshold"] >= 2:
        signals.append("{} transfers were just under {}, a common way of keeping payments below "
                       "reporting thresholds (structuring).".format(
                           f["n_just_under_threshold"], money(f["reporting_threshold_usd"])))
    # Uniform amounts: similar-sized payments look planned, not organic.
    if (f["n_transfers"] >= 3 and f["min_transfer_usd"] > 0
            and f["max_transfer_usd"] <= 1.25 * f["min_transfer_usd"]):
        signals.append("All {} transfers were similar in size, between {} and {}.".format(
            f["n_transfers"], money(f["min_transfer_usd"]), money(f["max_transfer_usd"])))
    return signals


def template_summary(f):
    """Build the 5-part summary from the facts pack `f`. Returns a dict of text."""
    key = f["key_account"]

    # 1. summary (2 sentences)
    s1 = "{n} accounts moved {total} in {t} transfers between {a} and {b}, activity consistent with {shape} laundering pattern.".format(
        n=f["n_accounts"], total=money(f["total_usd"]), t=f["n_transfers"],
        a=when(f["first_seen"]), b=when(f["last_seen"]), shape=shape_label(f["shape"]))
    if f["passed_on_pct"] is not None and f["hours_to_pass_on"] is not None:
        s2 = "Account {k} received {r} and passed on {p}% of it, starting within {h} hours.".format(
            k=key, r=money(f["key_received_usd"]), p=f["passed_on_pct"], h=f["hours_to_pass_on"])
    elif f["key_sent_usd"] > 0:
        s2 = "Account {k} sent {s} to {n} accounts.".format(
            k=key, s=money(f["key_sent_usd"]), n=f["key_payees"])
    else:
        s2 = "Account {k} received {r} from {n} accounts.".format(
            k=key, r=money(f["key_received_usd"]), n=f["key_senders"])

    # 2. what happened
    parts = ["In this ring, {}.".format(SHAPE_TEXT.get(f["shape"], UNKNOWN_SHAPE_TEXT))]
    if f["origin"]:
        parts.append("The money started at account {} ({} sent).".format(
            f["origin"], money(f["origin_sent_usd"])))
    if f["key_received_usd"] > 0 and f["key_sent_usd"] > 0:
        parts.append("Account {} received {} from {} account(s) and sent {} on to {} account(s).".format(
            key, money(f["key_received_usd"]), f["key_senders"],
            money(f["key_sent_usd"]), f["key_payees"]))
    elif f["key_sent_usd"] > 0:
        parts.append("Account {} sent {} to {} account(s).".format(
            key, money(f["key_sent_usd"]), f["key_payees"]))
    else:
        parts.append("Account {} received {} from {} account(s).".format(
            key, money(f["key_received_usd"]), f["key_senders"]))
    if f["collector"]:
        parts.append("Most of the money ended at account {} ({} received).".format(
            f["collector"], money(f["collector_received_usd"])))

    parts.append("This activity took place over {} hours.".format(f["duration_hours"]))
    what_happened = " ".join(parts)

    # 3. why suspicious: model scores first, then behaviour we can see in the transfers
    why = []
    if f["xgb_score"] is not None:
        why.append("The transfer model scored this activity {} out of 1.".format(f["xgb_score"]))
    if f["gnn_score"] is not None:
        why.append("The graph model, which looks at connected accounts, scored it {}.".format(f["gnn_score"]))
    if f["top_reasons"]:
        why.append("Main signals: {}.".format("; ".join(f["top_reasons"])))
    why.extend(behaviour_signals(f))
    if not why:
        why.append("These accounts are linked by a chain of connected transfers, which is why "
                   "the ring detector grouped them; no single strong signal stands out.")
    why_suspicious = " ".join(why)

    # 4. possible victim (the recruited mule)
    if f["passed_on_pct"] is not None and f["hours_to_pass_on"] is not None:
        possible_victim = ("Account {} forwarded {}% of what it received within {} hours, "
                           "which is typical of a recruited money mule. The account holder "
                           "may be a victim of a job or payment scam and should be treated "
                           "as a possible victim until reviewed.").format(
                               key, f["passed_on_pct"], f["hours_to_pass_on"])
    else:
        possible_victim = ("No account clearly passed money straight through. Account {} is "
                           "the main account in this ring; review whether its holder was "
                           "recruited before treating them as responsible.").format(key)

    # 5. suggested next step (the investigator decides, not the system)
    top_score = max([s for s in (f["xgb_score"], f["gnn_score"]) if s is not None] or [0])
    if top_score >= STRONG_SCORE:
        next_step = ("Consider freezing outgoing payments from account {} and escalating "
                     "the ring for review.").format(key)
    else:
        next_step = ("Review the history of account {} and request information from the "
                     "account holder before acting.").format(key)

    return {
        "summary": s1 + " " + s2,
        "what_happened": what_happened,
        "why_suspicious": why_suspicious,
        "possible_victim": possible_victim,
        "suggested_next_step": next_step,
    }
