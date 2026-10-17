"""Shared constants and small helpers that describe the transaction data.

Everything that needs to agree about column names, bank codes or account IDs
(cleaning, features, the dashboard, ...) should import it from here instead of
re-typing it.
"""

import re

import pandas as pd

# Exact header of HI-Small_Trans.csv, in file order.
# Note that "Account" appears twice: index 2 is the SENDER account and
# index 4 is the RECEIVER account.
RAW_COLUMNS = (
    "Timestamp",
    "From Bank",
    "Account",
    "To Bank",
    "Account",
    "Amount Received",
    "Receiving Currency",
    "Amount Paid",
    "Payment Currency",
    "Payment Format",
    "Is Laundering",
)

# Clean names, matched to RAW_COLUMNS by POSITION (never by pandas' automatic
# "Account.1" renaming of the duplicate header).
CLEAN_NAMES = (
    "ts",
    "from_bank",
    "from_acct_raw",
    "to_bank",
    "to_acct_raw",
    "amt_recv",
    "ccy_recv",
    "amt_paid",
    "ccy_paid",
    "fmt",
    "label",
)

TS_FORMAT = "%Y/%m/%d %H:%M"
USD = "US Dollar"
SPLIT_NAMES = ("train", "validation", "test")

# Column order of the final cleaned table.
CLEAN_COLUMN_ORDER = (
    "txn_id",
    "source_row",
    "ts",
    "from_bank",
    "from_bank_raw",
    "from_acct_raw",
    "from_acct",
    "to_bank",
    "to_bank_raw",
    "to_acct_raw",
    "to_acct",
    "amt_recv",
    "ccy_recv",
    "amt_paid",
    "ccy_paid",
    "amount_usd",
    "fmt",
    "label",
    "self_transfer",
    "split",
)

# Only the ASCII digits 0-9. (str.isdigit() would also accept things like
# Arabic-Indic or superscript digits, which we do not want.)
_BANK_PATTERN = re.compile(r"[0-9]+")
_WHITESPACE_PATTERN = re.compile(r"\s")


def canonical_bank(raw: str) -> str:
    """Turn a raw bank code into its canonical form.

    Leading zeros are removed so that "010" and "10" are the same bank.
    An all-zero code becomes "0".
    Raises ValueError if the code is empty or not made of ASCII digits.
    """
    if not isinstance(raw, str):
        raise ValueError(f"bank code must be a string, got {raw!r}")
    token = raw.strip()
    if not _BANK_PATTERN.fullmatch(token):
        raise ValueError(f"invalid bank code: {raw!r}")
    stripped = token.lstrip("0")
    if stripped == "":
        return "0"
    return stripped


def make_account_id(bank_raw: str, account_raw: str) -> str:
    """Build a globally unique account ID: "<canonical bank>_<account>".

    The same account text at two different banks gives two different IDs.
    The account text itself is never changed (leading zeros are kept).
    Raises ValueError if the account is empty or contains whitespace.
    """
    if not isinstance(account_raw, str) or account_raw == "":
        raise ValueError(f"account must be a non-empty string, got {account_raw!r}")
    if _WHITESPACE_PATTERN.search(account_raw):
        raise ValueError(f"account contains whitespace: {account_raw!r}")
    return f"{canonical_bank(bank_raw)}_{account_raw}"


def canonical_bank_series(s: pd.Series) -> pd.Series:
    """Vectorized version of canonical_bank.

    Assumes the values were already validated (ASCII digits after strip).
    """
    # A validated code is "optional outer whitespace + ASCII digits", so the
    # digit run is exactly the stripped code. Taking it with a regex avoids any
    # difference between Python's and pyarrow's idea of "whitespace".
    digits = s.astype("string").str.extract(r"([0-9]+)", expand=False)
    no_zeros = digits.str.lstrip("0")
    # An all-zero code becomes the empty string above; turn it into "0".
    return no_zeros.mask(no_zeros == "", "0")


def make_account_id_series(bank_s: pd.Series, acct_s: pd.Series) -> pd.Series:
    """Vectorized version of make_account_id.

    Assumes the values were already validated.
    """
    return canonical_bank_series(bank_s) + "_" + acct_s.astype("string")
