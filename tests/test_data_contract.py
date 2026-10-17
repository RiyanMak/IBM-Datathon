"""Tests for the shared bank / account helpers in common/data_contract.py."""

import pandas as pd
import pytest

from common.data_contract import (
    CLEAN_COLUMN_ORDER,
    CLEAN_NAMES,
    RAW_COLUMNS,
    canonical_bank,
    canonical_bank_series,
    make_account_id,
    make_account_id_series,
)


def test_column_tuples_have_matching_lengths():
    assert len(RAW_COLUMNS) == len(CLEAN_NAMES) == 11
    # "Account" really is duplicated in the raw header (sender, then receiver).
    assert RAW_COLUMNS[2] == RAW_COLUMNS[4] == "Account"
    assert CLEAN_NAMES[2] == "from_acct_raw" and CLEAN_NAMES[4] == "to_acct_raw"
    assert len(set(CLEAN_COLUMN_ORDER)) == len(CLEAN_COLUMN_ORDER) == 20


@pytest.mark.parametrize(
    "raw, expected",
    [("010", "10"), ("10", "10"), ("03208", "3208"), ("001", "1"),
     ("000", "0"), ("0", "0"), (" 0070 ", "70"), ("1124", "1124")],
)
def test_canonical_bank_examples(raw, expected):
    assert canonical_bank(raw) == expected


def test_leading_zeros_give_same_bank():
    assert canonical_bank("010") == canonical_bank("10") == canonical_bank("0010")


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "1a", "A10", "-10", "1.0", "1 0",
     "١٢",   # Arabic-Indic digits: isdigit() is True, but we reject them
     "²"],        # superscript two
)
def test_canonical_bank_rejects_bad_codes(bad):
    with pytest.raises(ValueError):
        canonical_bank(bad)


def test_account_leading_zeros_preserved():
    assert make_account_id("010", "0080AB") == "10_0080AB"
    assert make_account_id("1", "000000001") == "1_000000001"


def test_same_account_at_two_banks_gives_two_ids():
    a = make_account_id("010", "8000EBD30")
    b = make_account_id("011", "8000EBD30")
    assert a != b
    assert a == "10_8000EBD30" and b == "11_8000EBD30"


@pytest.mark.parametrize("bad_account", ["", " 8000EBD30", "8000EBD30 ", "8000 EBD30", "8000\tEBD30"])
def test_account_with_whitespace_or_empty_rejected(bad_account):
    with pytest.raises(ValueError):
        make_account_id("010", bad_account)


def test_bad_bank_rejected_in_account_id():
    with pytest.raises(ValueError):
        make_account_id("x10", "8000EBD30")


def test_scalar_and_vectorized_helpers_agree():
    banks = ["010", "10", "000", "0", "03208", "001", " 0070 ", "1124", "0000001", "70"]
    accts = ["8000EBD30", "0080AB", "NA", "NULL", "000000001", "8000F5340", "X", "abc", "0", "800737690"]

    expected_banks = [canonical_bank(b) for b in banks]
    expected_ids = [make_account_id(b, a) for b, a in zip(banks, accts)]

    for dtype in (object, "string", pd.StringDtype("pyarrow")):
        bank_s = pd.Series(banks, dtype=dtype)
        acct_s = pd.Series(accts, dtype=dtype)
        assert canonical_bank_series(bank_s).tolist() == expected_banks
        assert make_account_id_series(bank_s, acct_s).tolist() == expected_ids
