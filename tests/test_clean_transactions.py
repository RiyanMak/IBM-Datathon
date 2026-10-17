"""Tests for common/clean_transactions.py, using tiny CSV files written to tmp_path."""

import math

import pandas as pd
import pytest

from common.clean_transactions import (
    STRING_DTYPE,
    DataValidationError,
    load_raw,
    normalize,
    read_clean_csv,
    read_header,
    validate_raw,
)
from common.data_contract import CLEAN_NAMES

HEADER = (
    "Timestamp,From Bank,Account,To Bank,Account,Amount Received,"
    "Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering"
)

GOOD_ROWS = [
    "2022/09/01 00:20,010,8000EBD30,010,8000EBD30,3697.34,US Dollar,3697.34,US Dollar,Reinvestment,0",
    "2022/09/01 00:20,03208,8000F4580,001,8000F5340,0.01,US Dollar,0.01,US Dollar,Cheque,0",
    "2022/09/01 00:16,00220,8001C8C51,01420,8003093C1,0.025852,Bitcoin,0.025852,Bitcoin,Bitcoin,0",
]


def write_csv(tmp_path, rows, header=HEADER, name="trans.csv"):
    """Write a small CSV fixture and return its path."""
    path = tmp_path / name
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def make_row(i, ts="2022/09/01 00:00", from_bank="010", from_acct=None,
             to_bank="020", to_acct=None, amt="1.00", label="0"):
    """Build one raw CSV line; row i gets unique accounts by default."""
    from_acct = from_acct if from_acct is not None else f"A{i:08d}"
    to_acct = to_acct if to_acct is not None else f"B{i:08d}"
    return f"{ts},{from_bank},{from_acct},{to_bank},{to_acct},{amt},US Dollar,{amt},US Dollar,ACH,{label}"


def checks_found(problems):
    return {(p["check"], p["column"]) for p in problems}


def bad_rows(problems, check, column):
    for p in problems:
        if p["check"] == check and p["column"] == column:
            return [ex["source_row"] for ex in p["examples"]]
    return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_read_header(tmp_path):
    path = write_csv(tmp_path, GOOD_ROWS)
    assert read_header(path) == HEADER.split(",")


@pytest.mark.parametrize(
    "header",
    [
        HEADER.replace("Timestamp", "Time"),
        HEADER + ",Extra",
        HEADER.replace(",Is Laundering", ""),
        HEADER.replace("From Bank,Account", "Account,From Bank"),
    ],
)
def test_header_mismatch_raises(tmp_path, header):
    path = write_csv(tmp_path, GOOD_ROWS, header=header)
    with pytest.raises(DataValidationError) as info:
        load_raw(path)
    assert info.value.problems[0]["check"] == "header"


def test_columns_renamed_by_position_and_kept_as_text(tmp_path):
    raw = load_raw(write_csv(tmp_path, GOOD_ROWS))
    assert list(raw.columns) == ["source_row", *CLEAN_NAMES]
    # Nothing converted or trimmed on load.
    assert raw.loc[1, "from_bank"] == "03208"
    assert raw.loc[1, "to_bank"] == "001"
    assert raw.loc[2, "amt_recv"] == "0.025852"
    assert raw.loc[0, "label"] == "0"


def test_sender_and_receiver_not_swapped(tmp_path):
    raw = load_raw(write_csv(tmp_path, GOOD_ROWS))
    assert raw.loc[1, "from_acct_raw"] == "8000F4580"
    assert raw.loc[1, "to_acct_raw"] == "8000F5340"
    clean = normalize(raw)
    assert clean.loc[1, "from_acct"] == "3208_8000F4580"
    assert clean.loc[1, "to_acct"] == "1_8000F5340"
    assert clean.loc[2, "from_acct"] == "220_8001C8C51"
    assert clean.loc[2, "to_acct"] == "1420_8003093C1"


def test_source_row_with_nrows(tmp_path):
    rows = [make_row(i) for i in range(10)]
    raw = load_raw(write_csv(tmp_path, rows), nrows=4)
    assert raw["source_row"].tolist() == [0, 1, 2, 3]
    assert raw["source_row"].dtype == "int64"
    assert raw["from_acct_raw"].tolist() == ["A00000000", "A00000001", "A00000002", "A00000003"]


def test_source_row_with_sample_every(tmp_path):
    rows = [make_row(i) for i in range(10)]
    raw = load_raw(write_csv(tmp_path, rows), sample_every=3)
    assert raw["source_row"].tolist() == [0, 3, 6, 9]
    # The content must be the raw row at that position.
    assert raw["from_acct_raw"].tolist() == ["A00000000", "A00000003", "A00000006", "A00000009"]
    assert raw["to_acct_raw"].tolist() == ["B00000000", "B00000003", "B00000006", "B00000009"]


def test_sample_every_one_keeps_everything(tmp_path):
    rows = [make_row(i) for i in range(5)]
    raw = load_raw(write_csv(tmp_path, rows), sample_every=1)
    assert raw["source_row"].tolist() == [0, 1, 2, 3, 4]


def test_nrows_and_sample_every_together_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_raw(write_csv(tmp_path, GOOD_ROWS), nrows=2, sample_every=2)


def test_na_and_null_text_kept_literally(tmp_path):
    rows = [make_row(0, from_acct="NA", to_acct="NULL"), make_row(1, from_acct="nan", to_acct="N/A")]
    raw = load_raw(write_csv(tmp_path, rows))
    assert not raw.isna().any().any()
    assert raw["from_acct_raw"].tolist() == ["NA", "nan"]
    assert raw["to_acct_raw"].tolist() == ["NULL", "N/A"]
    clean = normalize(raw)
    assert clean["from_acct"].tolist() == ["10_NA", "10_nan"]
    assert clean["to_acct"].tolist() == ["20_NULL", "20_N/A"]


# ---------------------------------------------------------------------------
# Validation problems
# ---------------------------------------------------------------------------

def test_good_rows_have_no_problems(tmp_path):
    assert validate_raw(load_raw(write_csv(tmp_path, GOOD_ROWS))) == []


def test_empty_cell_detected(tmp_path):
    rows = [make_row(0), make_row(1, from_bank=""), make_row(2, to_acct="")]
    raw = load_raw(write_csv(tmp_path, rows))
    problems = validate_raw(raw)
    assert bad_rows(problems, "bank_code", "from_bank") == [1]
    assert bad_rows(problems, "account", "to_acct_raw") == [2]


def test_short_row_detected_as_missing_field(tmp_path):
    full = make_row(1)
    short = ",".join(full.split(",")[:8])  # only 8 of 11 fields
    rows = [make_row(0), short, make_row(2)]
    raw = load_raw(write_csv(tmp_path, rows))
    problems = validate_raw(raw)
    for col in ("ccy_paid", "fmt", "label"):
        assert bad_rows(problems, "missing_field", col) == [1]
    # The 8 present fields are not reported as missing.
    assert bad_rows(problems, "missing_field", "amt_paid") is None
    with pytest.raises(DataValidationError) as info:
        normalize(raw)
    assert "missing_field" in str(info.value)
    assert "source_row [1]" in str(info.value)


def test_short_row_detected_when_sampling(tmp_path):
    rows = [make_row(i) for i in range(7)]
    rows[4] = ",".join(rows[4].split(",")[:10])  # drop the label
    raw = load_raw(write_csv(tmp_path, rows), sample_every=2)
    assert raw["source_row"].tolist() == [0, 2, 4, 6]
    assert bad_rows(validate_raw(raw), "missing_field", "label") == [4]


def test_blank_line_does_not_shift_source_row(tmp_path):
    rows = [make_row(0), "", make_row(2)]
    raw = load_raw(write_csv(tmp_path, rows))
    assert raw["source_row"].tolist() == [0, 1, 2]
    assert raw.loc[2, "from_acct_raw"] == "A00000002"
    assert bad_rows(validate_raw(raw), "missing_field", "ts") == [1]


def test_too_many_fields_raises(tmp_path):
    rows = [make_row(0), make_row(1) + ",extra"]
    with pytest.raises(DataValidationError) as info:
        load_raw(write_csv(tmp_path, rows))
    problem = info.value.problems[0]
    assert problem["check"] == "too_many_fields"
    assert problem["examples"][0]["source_row"] == 1


@pytest.mark.parametrize(
    "bad_ts",
    ["2022-09-01 00:20", "2022/9/1 00:20", "2022/09/01 00:20:00", "2022/09/01",
     "2022/02/30 10:00", "2022/09/01 25:00", "2022/13/01 00:00", "garbage"],
)
def test_bad_timestamp_detected(tmp_path, bad_ts):
    rows = [make_row(0), make_row(1, ts=bad_ts)]
    raw = load_raw(write_csv(tmp_path, rows))
    assert bad_rows(validate_raw(raw), "timestamp", "ts") == [1]
    with pytest.raises(DataValidationError) as info:
        normalize(raw)
    assert info.value.problems[0]["examples"][0] == {"source_row": 1, "raw_value": bad_ts}


@pytest.mark.parametrize("bad_amt", ["-5.00", "inf", "nan", "1e5", "", "1.", ".5", " 1.0", "1" * 400])
def test_bad_amount_detected(tmp_path, bad_amt):
    # "1" * 400 is valid text but overflows to inf as a float.
    rows = [make_row(0), make_row(1), make_row(2, amt=bad_amt)]
    raw = load_raw(write_csv(tmp_path, rows))
    problems = validate_raw(raw)
    assert bad_rows(problems, "amount", "amt_recv") == [2]
    assert bad_rows(problems, "amount", "amt_paid") == [2]
    with pytest.raises(DataValidationError):
        normalize(raw)


@pytest.mark.parametrize("bad_label", ["2", "-1", "1.0", "", "yes", " 1"])
def test_bad_label_detected(tmp_path, bad_label):
    rows = [make_row(0), make_row(1, label=bad_label)]
    raw = load_raw(write_csv(tmp_path, rows))
    assert bad_rows(validate_raw(raw), "label", "label") == [1]
    with pytest.raises(DataValidationError) as info:
        normalize(raw)
    assert "label" in str(info.value)


@pytest.mark.parametrize("bad_bank", ["A10", "1.0", "١٢", "-1"])
def test_bad_bank_detected(tmp_path, bad_bank):
    rows = [make_row(0, to_bank=bad_bank)]
    raw = load_raw(write_csv(tmp_path, rows))
    assert bad_rows(validate_raw(raw), "bank_code", "to_bank") == [0]


def test_account_with_inner_space_detected(tmp_path):
    rows = [make_row(0, from_acct="8000 EBD30")]
    raw = load_raw(write_csv(tmp_path, rows))
    assert bad_rows(validate_raw(raw), "account", "from_acct_raw") == [0]


def test_examples_capped_at_20(tmp_path):
    rows = [make_row(i, label="9") for i in range(30)]
    problems = validate_raw(load_raw(write_csv(tmp_path, rows)))
    assert problems[0]["count"] == 30
    assert len(problems[0]["examples"]) == 20


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_bitcoin_amount_parses_exactly(tmp_path):
    clean = normalize(load_raw(write_csv(tmp_path, GOOD_ROWS)))
    assert clean.loc[2, "amt_recv"] == 0.025852
    assert clean.loc[2, "amt_paid"] == float("0.025852")
    assert clean.loc[0, "amt_recv"] == 3697.34


def test_large_and_zero_amounts_kept_unrounded(tmp_path):
    rows = [make_row(0, amt="4197895233.69"), make_row(1, amt="0.000004"), make_row(2, amt="0")]
    clean = normalize(load_raw(write_csv(tmp_path, rows)))
    assert clean["amt_recv"].tolist() == [4197895233.69, 0.000004, 0.0]
    assert len(clean) == 3


def test_positive_self_transfer_keeps_label(tmp_path):
    rows = [make_row(0, from_bank="010", from_acct="8000EBD30",
                     to_bank="10", to_acct="8000EBD30", label="1"),
            make_row(1)]
    clean = normalize(load_raw(write_csv(tmp_path, rows)))
    assert clean["self_transfer"].tolist() == [True, False]
    assert clean["label"].tolist() == [1, 0]
    assert len(clean) == 2


def test_same_account_text_at_different_banks_is_not_self_transfer(tmp_path):
    rows = [make_row(0, from_bank="010", from_acct="X1", to_bank="011", to_acct="X1")]
    clean = normalize(load_raw(write_csv(tmp_path, rows)))
    assert clean["self_transfer"].tolist() == [False]


def test_exact_duplicates_are_both_kept(tmp_path):
    rows = [GOOD_ROWS[1], GOOD_ROWS[0], GOOD_ROWS[1]]
    clean = normalize(load_raw(write_csv(tmp_path, rows)))
    assert len(clean) == 3
    assert clean["source_row"].tolist() == [0, 1, 2]
    dup = clean[clean["from_acct"] == "3208_8000F4580"]
    assert dup["source_row"].tolist() == [0, 2]


def test_row_order_unchanged(tmp_path):
    # The fixture is not time-ordered; normalize must not sort it.
    clean = normalize(load_raw(write_csv(tmp_path, GOOD_ROWS)))
    assert clean["ts"].tolist() == [pd.Timestamp("2022-09-01 00:20"),
                                    pd.Timestamp("2022-09-01 00:20"),
                                    pd.Timestamp("2022-09-01 00:16")]


def test_ts_has_no_timezone(tmp_path):
    clean = normalize(load_raw(write_csv(tmp_path, GOOD_ROWS)))
    assert clean["ts"].dt.tz is None
    assert clean["ts"].dtype == "datetime64[ns]"
    assert clean.loc[0, "ts"] == pd.Timestamp(2022, 9, 1, 0, 20)


def test_normalized_dtypes(tmp_path):
    clean = normalize(load_raw(write_csv(tmp_path, GOOD_ROWS)))
    expected = {
        "source_row": "int64",
        "ts": "datetime64[ns]",
        "amt_recv": "float64",
        "amt_paid": "float64",
        "label": "int8",
        "self_transfer": "bool",
    }
    for col, dtype in expected.items():
        assert clean[col].dtype == dtype, col
    for col in ("from_bank", "to_bank", "from_acct_raw", "to_acct_raw", "from_acct", "to_acct"):
        assert clean[col].dtype == STRING_DTYPE, col
    for col in ("from_bank_raw", "to_bank_raw", "ccy_recv", "ccy_paid", "fmt"):
        assert isinstance(clean[col].dtype, pd.CategoricalDtype), col


def test_bank_columns_raw_and_canonical(tmp_path):
    clean = normalize(load_raw(write_csv(tmp_path, GOOD_ROWS)))
    assert clean["from_bank_raw"].astype(str).tolist() == ["010", "03208", "00220"]
    assert clean["from_bank"].tolist() == ["10", "3208", "220"]
    assert clean["to_bank_raw"].astype(str).tolist() == ["010", "001", "01420"]
    assert clean["to_bank"].tolist() == ["10", "1", "1420"]


def test_normalize_returns_new_frame(tmp_path):
    raw = load_raw(write_csv(tmp_path, GOOD_ROWS))
    before = raw.copy()
    clean = normalize(raw)
    assert clean is not raw
    assert raw.equals(before)


def test_read_clean_csv_column_subset(tmp_path):
    clean = normalize(load_raw(write_csv(tmp_path, GOOD_ROWS)))
    out = tmp_path / "clean.csv"
    clean.to_csv(out, index=False)
    cols = ["ts", "source_row", "from_acct", "amt_paid", "ccy_paid"]
    part = read_clean_csv(out, columns=cols)
    assert list(part.columns) == cols
    assert part.equals(clean[cols])
    with pytest.raises(ValueError, match="no_such_column"):
        read_clean_csv(out, columns=["ts", "no_such_column"])


def test_csv_round_trip(tmp_path):
    rows = GOOD_ROWS + [make_row(3, amt="4197895233.69", label="1"),
                        make_row(4, from_acct="0080AB", amt="0.000004")]
    clean = normalize(load_raw(write_csv(tmp_path, rows)))
    out = tmp_path / "clean.csv"
    clean.to_csv(out, index=False)
    reloaded = read_clean_csv(out)
    assert reloaded.equals(clean)
    assert (reloaded.dtypes == clean.dtypes).all()
    assert reloaded.loc[4, "from_acct_raw"] == "0080AB"
    assert not math.isnan(reloaded.loc[4, "amt_recv"])
