"""End-to-end tests for scripts/clean_data.py and scripts/validate_clean.py.

Each test writes a tiny CSV into tmp_path and calls the scripts' main(argv).
"""

import json

import pandas as pd
import pytest

from common.clean_transactions import read_clean_csv
from common.data_contract import CLEAN_COLUMN_ORDER
from scripts import clean_data, validate_clean

HEADER = ("Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,"
          "Amount Paid,Payment Currency,Payment Format,Is Laundering")
USD = "US Dollar"


def good_rows():
    """40 rows, one per minute. The first 24 (train) carry FX evidence for
    Euro (rate 1.2) and Bitcoin (rate 10000) in both directions."""
    rows = []
    for i in range(40):
        ts = f"2022/09/01 {i // 60:02d}:{i % 60:02d}"
        kind = i % 5
        if kind == 0:     # paid Euro, received USD
            rec = ("010", f"A{i}", "020", f"B{i}", "120.00", USD, "100.00", "Euro", "Wire")
        elif kind == 1:   # paid USD, received Euro
            rec = ("010", f"A{i}", "0020", f"B{i}", "50.00", "Euro", "60.00", USD, "ACH")
        elif kind == 2:   # paid Bitcoin, received USD
            rec = ("030", f"C{i}", "010", f"A{i}", "10000.00", USD, "1.000000", "Bitcoin", "Bitcoin")
        elif kind == 3:   # paid USD, received Bitcoin
            rec = ("010", f"A{i}", "030", f"C{i}", "2.000000", "Bitcoin", "20000.00", USD, "Bitcoin")
        else:             # plain USD self-transfer
            rec = ("010", "SELF", "10", "SELF", "5.00", USD, "5.00", USD, "Cash")
        label = "1" if i in (3, 17, 31) else "0"
        rows.append(",".join((ts,) + rec + (label,)))
    return rows


def write_csv(path, rows):
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def run_clean(csv_path, out_dir, *extra):
    return clean_data.main(["--input", str(csv_path), "--out", str(out_dir), *extra])


def test_good_fixture_end_to_end(tmp_path):
    csv_path = write_csv(tmp_path / "good.csv", good_rows())
    out = tmp_path / "out"
    assert run_clean(csv_path, out) == 0

    for name in ("clean.csv", "splits.json", "fx_rates.json", "data_quality.md"):
        assert (out / name).exists()
    assert not list(out.glob(".*.tmp"))  # no temporary files left behind

    df = read_clean_csv(out / "clean.csv")
    assert tuple(df.columns) == CLEAN_COLUMN_ORDER
    assert len(df) == 40
    assert df["txn_id"].tolist() == list(range(40))

    splits = json.loads((out / "splits.json").read_text())
    assert splits["total_rows"] == 40
    fx = json.loads((out / "fx_rates.json").read_text())
    assert fx["rates"]["Euro"]["rate"] == pytest.approx(1.2)
    assert fx["rates"]["Bitcoin"]["rate"] == pytest.approx(10000.0)
    assert fx["fit_cutoff_exclusive"] == splits["validation_start"]

    report = (out / "data_quality.md").read_text()
    assert "**Overall status: PASS**" in report
    assert "SAMPLE RUN" not in report

    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps({USD: 1.0, "Euro": 1.2, "Bitcoin": 10000.0}))
    code = validate_clean.main(["--out", str(out), "--input", str(csv_path),
                                "--reference-rates", str(ref)])
    assert code == 0
    validation = (out / "validation.md").read_text()
    assert "**Overall status: PASS**" in validation
    assert "not applicable (different input)" in validation


def test_stride_sample_writes_sample_files(tmp_path):
    csv_path = write_csv(tmp_path / "good.csv", good_rows())
    out = tmp_path / "out"
    # Every row with an even source_row: still has Euro and Bitcoin evidence.
    assert run_clean(csv_path, out, "--sample-every", "2") == 0
    assert (out / "clean_sample.csv").exists()
    assert not (out / "clean.csv").exists()
    assert "SAMPLE RUN" in (out / "data_quality.md").read_text()
    assert validate_clean.main(["--out", str(out), "--input", str(csv_path)]) == 0


def test_bad_timestamp_fails_with_report(tmp_path):
    rows = good_rows()
    rows[7] = rows[7].replace("2022/09/01 00:07", "2022/13/01 00:07")
    csv_path = write_csv(tmp_path / "bad_ts.csv", rows)
    out = tmp_path / "out"
    assert run_clean(csv_path, out) == 1

    report = (out / "data_quality.md").read_text()
    assert "FAILED" in report
    assert "normalize" in report
    assert "| 7 | 2022/13/01 00:07 |" in report  # source_row and the raw value
    assert not (out / "clean.csv").exists()
    assert not (out / "splits.json").exists()
    assert not (out / "fx_rates.json").exists()


def test_currency_without_training_evidence_fails(tmp_path):
    rows = good_rows()
    # Yen only appears in the last (test) rows, never next to US Dollar.
    rows[38] = "2022/09/01 00:38,010,Y1,010,Y2,500.00,Yen,500.00,Yen,Wire,0"
    csv_path = write_csv(tmp_path / "yen.csv", rows)
    out = tmp_path / "out"
    assert run_clean(csv_path, out) == 1

    report = (out / "data_quality.md").read_text()
    assert "FAILED" in report
    assert "fx_fit" in report
    assert "Yen" in report
    assert not (out / "clean.csv").exists()
    assert not (out / "fx_rates.json").exists()


def test_refuses_to_overwrite_without_flag(tmp_path):
    csv_path = write_csv(tmp_path / "good.csv", good_rows())
    out = tmp_path / "out"
    assert run_clean(csv_path, out) == 0
    before = (out / "clean.csv").stat().st_mtime_ns

    assert run_clean(csv_path, out) == 2
    assert (out / "clean.csv").stat().st_mtime_ns == before

    assert run_clean(csv_path, out, "--overwrite") == 0


def test_validator_detects_tampered_label(tmp_path):
    csv_path = write_csv(tmp_path / "good.csv", good_rows())
    out = tmp_path / "out"
    assert run_clean(csv_path, out) == 0

    df = read_clean_csv(out / "clean.csv")
    df.loc[0, "label"] = 1 - df.loc[0, "label"]  # flip one label, keep the dtype
    assert df["label"].dtype == "int8"
    df.to_csv(out / "clean.csv", index=False)

    assert validate_clean.main(["--out", str(out), "--input", str(csv_path)]) == 1
    assert "**Overall status: FAIL**" in (out / "validation.md").read_text()
