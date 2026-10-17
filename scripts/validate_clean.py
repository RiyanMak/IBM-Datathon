"""Independent checker for the outputs of scripts/clean_data.py.

Usage (from the repo root):

    python -m scripts.validate_clean --out data/processed --input HI-Small_Trans.csv \
        [--reference-rates ~/Downloads/rates.json]

INDEPENDENCE: this file deliberately does NOT import common.clean_transactions,
common.splits, common.fx or common.quality. Everything is re-derived here with
separate, simple code (the csv module for the raw file, hashlib for the hash,
pandas/numpy only to read the outputs). Only constants are taken from
common.data_contract, because they ARE the agreed contract.

Writes validation.md into --out. Exit code 1 if any check is FAIL.
"""

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from common.clean_transactions import read_clean_csv
from common.data_contract import CLEAN_COLUMN_ORDER, RAW_COLUMNS, SPLIT_NAMES, USD

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"

RAW_TS_FORMAT = "%Y/%m/%d %H:%M"
SPOT_CHECK_SEED = 42
SPOT_CHECK_COUNT = 25
KNOWN_ROW = 318130  # a known positive self-transfer in the full file
FX_RELATIVE_TOLERANCE = 1e-12
REFERENCE_ABS_TOLERANCE = 5e-7  # half of the 6th decimal of a rounded rate

EXPECTED_DTYPES = {
    "txn_id": "int64",
    "source_row": "int64",
    "ts": "datetime64[ns]",
    "from_bank": "string",
    "from_bank_raw": "category",
    "from_acct_raw": "string",
    "from_acct": "string",
    "to_bank": "string",
    "to_bank_raw": "category",
    "to_acct_raw": "string",
    "to_acct": "string",
    "amt_recv": "float64",
    "ccy_recv": "category",
    "amt_paid": "float64",
    "ccy_paid": "category",
    "amount_usd": "float64",
    "fmt": "category",
    "label": "int8",
    "self_transfer": "bool",
    "split": "category",
}

# ---------------------------------------------------------------------------
# Kevin's reference results (computed independently by a teammate on the full
# HI-Small_Trans.csv). Only compared in full mode AND when the input SHA-256
# matches his. A mismatch is a FAIL; the data is never edited to match.
# ---------------------------------------------------------------------------
KEVIN_REFERENCE = {
    "input_sha256": "b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040",
    "rows": 5_078_345,
    "positives": 5_177,
    "negatives": 5_073_168,
    "ts_min": "2022-09-01T00:00:00",
    "ts_max": "2022-09-18T16:18:00",
    "rows_after_2022_09_10": 1_108,
    "positives_after_2022_09_10": 655,
    "self_transfers": 591_212,
    "positive_self_transfers": 11,
    "duplicate_groups": 9,
    "duplicate_rows": 18,
    "currencies": 15,
    "payment_formats": 7,
    "cross_currency_rows": 72_170,
    "fx_training_observations": 34_074,
    "fx_non_usd_currencies": 14,
    "validation_start": "2022-09-06T13:36:00",
    "test_start": "2022-09-08T16:12:00",
    "splits": {
        "train": {"rows": 3_046_861, "positives": 2_297, "txn_id_min": 0, "txn_id_max": 3_046_860},
        "validation": {"rows": 1_015_602, "positives": 1_082,
                       "txn_id_min": 3_046_861, "txn_id_max": 4_062_462},
        "test": {"rows": 1_015_882, "positives": 1_798,
                 "txn_id_min": 4_062_463, "txn_id_max": 5_078_344},
    },
    # currency: (rate in USD per unit, number of training observations)
    "rates": {
        "Australian Dollar": (0.70781426943, 858),
        "Bitcoin": (11875.0, 841),
        "Brazil Real": (0.177100858808, 453),
        "Canadian Dollar": (0.757977715119, 1146),
        "Euro": (1.17178345372, 14819),
        "Mexican Peso": (0.0472967538871, 662),
        "Ruble": (0.0128528096233, 1160),
        "Rupee": (0.0136158161315, 1705),
        "Saudi Riyal": (0.266588467938, 495),
        "Shekel": (0.296120815374, 1242),
        "Swiss Franc": (1.09289617443, 1525),
        "UK Pound": (1.29165590259, 2382),
        "Yen": (0.00948766603329, 1682),
        "Yuan": (0.149307213983, 5104),
    },
    "rate_relative_tolerance": 1e-9,  # his values have about 12 significant digits
}

DUPLICATE_KEY = ("ts", "from_bank_raw", "from_acct_raw", "to_bank_raw", "to_acct_raw",
                 "amt_recv", "ccy_recv", "amt_paid", "ccy_paid", "fmt", "label")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Checks:
    """Collects check results grouped in sections."""

    def __init__(self):
        self.sections = []  # list of (title, [check dicts], extra markdown)

    def start(self, title):
        self.sections.append([title, [], ""])

    def add(self, name, measured, rule, status):
        self.sections[-1][1].append(
            {"name": name, "measured": str(measured), "rule": rule, "status": status})

    def ok(self, name, condition, measured, rule):
        self.add(name, measured, rule, PASS if condition else FAIL)

    def extra(self, text):
        self.sections[-1][2] += text + "\n\n"

    def statuses(self):
        return [c["status"] for _, checks, _ in self.sections for c in checks]


def md_table(header, rows):
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(cell(h) for h in header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def iso(ts):
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def text_values(series):
    """Column values as a numpy array of Python strings."""
    return np.asarray(series.astype(object).to_numpy(), dtype=object)


def kept_by_mode(source_row, mode, nrows, sample_every):
    if mode == "head_sample":
        return source_row < nrows
    if mode == "stride_sample":
        return source_row % sample_every == 0
    return True


def scan_raw(path, mode, nrows, sample_every, wanted_rows):
    """ONE pass over the raw CSV with the csv module.

    Returns (header, total data rows in file, kept rows, kept label counts,
    {source_row: raw fields} for the wanted rows).
    """
    kept = 0
    labels = {}
    total = 0
    fetched = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        for i, fields in enumerate(reader):
            total += 1
            if not kept_by_mode(i, mode, nrows, sample_every):
                continue
            kept += 1
            label = fields[10] if len(fields) > 10 else "<missing>"
            labels[label] = labels.get(label, 0) + 1
            if i in wanted_rows:
                fetched[i] = fields
    return header, total, kept, labels, fetched


# ---------------------------------------------------------------------------
# Check groups
# ---------------------------------------------------------------------------

def check_schema(c, df):
    c.start("Schema")
    cols = tuple(df.columns)
    c.ok("Columns exactly CLEAN_COLUMN_ORDER", cols == CLEAN_COLUMN_ORDER,
         f"{len(cols)} columns", "same names, same order")
    wrong = [f"{col}: {df[col].dtype}" for col, want in EXPECTED_DTYPES.items()
             if col in df.columns and str(df[col].dtype) != want]
    c.ok("Column dtypes", not wrong, "wrong: " + (", ".join(wrong) or "none"),
         "as in the schema (int64, datetime64[ns], string, category, float64, int8, bool)")
    if "split" in df.columns and str(df["split"].dtype) == "category":
        cats = list(df["split"].cat.categories)
        c.ok("split categories", cats == list(SPLIT_NAMES) and df["split"].cat.ordered,
             f"{cats}, ordered={df['split'].cat.ordered}", f"{list(SPLIT_NAMES)}, ordered")


def check_ids_and_order(c, df, mode, nrows, sample_every):
    c.start("Identifiers and ordering")
    n = len(df)
    txn = df["txn_id"].to_numpy()
    c.ok("txn_id == 0..N-1", np.array_equal(txn, np.arange(n)), f"N = {n:,}", "contiguous, in row order")
    sr = df["source_row"].to_numpy()
    n_unique = len(np.unique(sr))
    c.ok("source_row unique", n_unique == n, f"{n_unique:,} unique of {n:,}", "no duplicates")
    if mode == "full":
        c.ok("source_row set == 0..N-1", np.array_equal(np.sort(sr), np.arange(n)),
             f"min {sr.min()}, max {sr.max()}", "every raw row exactly once")
    elif mode == "stride_sample":
        bad = int((sr % sample_every != 0).sum())
        c.ok(f"source_row % {sample_every} == 0", bad == 0, f"{bad} rows break it", "stride sample")
    else:
        bad = int((sr >= nrows).sum())
        c.ok(f"source_row < {nrows}", bad == 0, f"{bad} rows break it", "head sample")

    # Sorted by (ts, source_row): compare each row with the next one.
    ts = df["ts"].to_numpy().astype("int64")
    later = ts[1:] > ts[:-1]
    tie_ok = (ts[1:] == ts[:-1]) & (sr[1:] > sr[:-1])
    bad = int((~(later | tie_ok)).sum())
    c.ok("Rows sorted by (ts, source_row)", bad == 0, f"{bad} out-of-order neighbours",
         "ts ascending, ties by source_row ascending")


def check_splits(c, df, splits):
    c.start("Splits")
    v_start = pd.Timestamp(splits["validation_start"])
    t_start = pd.Timestamp(splits["test_start"])
    ts = df["ts"].to_numpy()
    expected = np.where(ts < np.datetime64(v_start), "train",
                        np.where(ts < np.datetime64(t_start), "validation", "test"))
    actual = text_values(df["split"])
    n_bad = int((actual != expected).sum())
    c.ok("Each row's split matches the cutoffs", n_bad == 0, f"{n_bad} rows differ",
         "train: ts < validation_start; validation: < test_start; test: rest")

    # Re-derive the cutoffs from the documented method (positions 60% / 80%).
    sorted_ts = np.sort(ts)
    n = len(sorted_ts)
    v_re, t_re = iso(sorted_ts[n * 6 // 10]), iso(sorted_ts[n * 8 // 10])
    c.ok("Cutoffs re-derived from floor(0.6N), floor(0.8N)",
         (v_re, t_re) == (splits["validation_start"], splits["test_start"]),
         f"{v_re} / {t_re}", f"{splits['validation_start']} / {splits['test_start']} (splits.json)")

    labels = df["label"].to_numpy()
    txn = df["txn_id"].to_numpy()
    rows = []
    for name in SPLIT_NAMES:
        mask = expected == name
        got = {
            "rows": int(mask.sum()),
            "positives": int((labels[mask] == 1).sum()),
            "negatives": int((labels[mask] == 0).sum()),
            "txn_id_min": int(txn[mask].min()) if mask.any() else None,
            "txn_id_max": int(txn[mask].max()) if mask.any() else None,
            "ts_min": iso(ts[mask].min()) if mask.any() else None,
            "ts_max": iso(ts[mask].max()) if mask.any() else None,
        }
        want = {k: splits["summary"][name][k] for k in got}
        c.ok(f"{name} summary equals splits.json", got == want,
             f"{got['rows']:,} rows, {got['positives']:,} positives", "all summary fields equal")
        rows.append([name, f"{got['rows']:,}", f"{got['positives']:,}",
                     f"{got['txn_id_min']}..{got['txn_id_max']}", f"{got['ts_min']} .. {got['ts_max']}"])
    c.extra(md_table(["Split", "Rows", "Positives", "txn_id", "ts"], rows))

    per_ts = pd.DataFrame({"ts": ts, "split": actual}).groupby("ts")["split"].nunique()
    shared = int((per_ts > 1).sum())
    c.ok("No timestamp in two splits", shared == 0, f"{shared} timestamps shared", "0")


def check_raw(c, df, input_path, mode, nrows, sample_every, sha):
    """Raw conservation and spot checks, in one csv pass."""
    source_rows = df["source_row"].to_numpy()
    rng = np.random.default_rng(SPOT_CHECK_SEED)
    size = min(SPOT_CHECK_COUNT, len(source_rows))
    spot = [int(r) for r in rng.choice(source_rows, size=size, replace=False)]
    if KNOWN_ROW in set(source_rows.tolist()) and KNOWN_ROW not in spot:
        spot.append(KNOWN_ROW)

    header, total, kept, raw_labels, fetched = scan_raw(input_path, mode, nrows, sample_every, set(spot))

    c.start("Raw conservation")
    c.ok("Raw header", tuple(header) == RAW_COLUMNS, ",".join(header)[:60] + "...", "RAW_COLUMNS")
    c.add("Raw data rows in file", f"{total:,}", "counted with the csv module", INFO)
    c.ok("Rows kept by the mode == clean rows", kept == len(df), f"raw {kept:,} / clean {len(df):,}",
         "equal")
    pq_pos = int((df["label"] == 1).sum())
    pq_neg = int((df["label"] == 0).sum())
    c.ok("Label 1 count", raw_labels.get("1", 0) == pq_pos,
         f"raw {raw_labels.get('1', 0):,} / clean {pq_pos:,}", "equal")
    c.ok("Label 0 count", raw_labels.get("0", 0) == pq_neg,
         f"raw {raw_labels.get('0', 0):,} / clean {pq_neg:,}", "equal")
    if mode == "full":
        want = np.arange(total)
    elif mode == "stride_sample":
        want = np.arange(0, total, sample_every)
    else:
        want = np.arange(min(nrows, total))
    c.ok("source_row set == rows the mode keeps", np.array_equal(np.sort(source_rows), want),
         f"{len(want):,} expected", "same set")

    c.start("Raw spot checks")
    sub = df[df["source_row"].isin(spot)].set_index("source_row")
    failures = []
    shown = []
    for r in spot:
        raw = fetched.get(r)
        if raw is None:
            failures.append(f"{r}: raw line not found")
            continue
        row = sub.loc[r]
        from_acct = f"{int(raw[1])}_{raw[2]}"
        to_acct = f"{int(raw[3])}_{raw[4]}"
        expected = {
            "ts": datetime.strptime(raw[0], RAW_TS_FORMAT),
            "from_bank": str(int(raw[1])),
            "from_bank_raw": raw[1],
            "from_acct_raw": raw[2],
            "from_acct": from_acct,
            "to_bank": str(int(raw[3])),
            "to_bank_raw": raw[3],
            "to_acct_raw": raw[4],
            "to_acct": to_acct,
            "amt_recv": float(raw[5]),
            "ccy_recv": raw[6],
            "amt_paid": float(raw[7]),
            "ccy_paid": raw[8],
            "fmt": raw[9],
            "label": int(raw[10]),
            "self_transfer": from_acct == to_acct,
        }
        actual = {k: row[k] for k in expected}
        actual["ts"] = pd.Timestamp(actual["ts"]).to_pydatetime()
        actual["label"] = int(actual["label"])
        actual["self_transfer"] = bool(actual["self_transfer"])
        actual["amt_recv"] = float(actual["amt_recv"])
        actual["amt_paid"] = float(actual["amt_paid"])
        for k in expected:
            if k not in ("ts", "label", "self_transfer", "amt_recv", "amt_paid"):
                actual[k] = str(actual[k])
        diffs = [k for k in expected if actual[k] != expected[k]]
        if diffs:
            failures.append(f"{r}: {diffs}")
        if len(shown) < 5:
            shown.append([r, raw[0], iso(actual["ts"]), raw[1], actual["from_bank"], actual["from_acct"],
                          raw[7], repr(actual["amt_paid"]), raw[10], actual["label"],
                          "all 16 fields equal" if not diffs else f"DIFF {diffs}"])
    c.ok("Spot-checked rows equal the raw lines", not failures,
         f"{len(spot)} rows checked, {len(failures)} differ" + (f": {failures[:5]}" if failures else ""),
         f"{size} random rows (default_rng({SPOT_CHECK_SEED})) + row {KNOWN_ROW} if present; 16 fields each")
    c.extra("First 5 spot checks:\n\n" + md_table(
        ["source_row", "raw ts", "ts", "raw from bank", "from_bank", "from_acct", "raw amt_paid",
         "amt_paid", "raw label", "label", "result"], shown))

    c.start(f"Known row {KNOWN_ROW}")
    if mode != "full":
        c.add(f"source_row {KNOWN_ROW}", "not in sample" if KNOWN_ROW not in sub.index else "in sample",
              "only checked in full mode", INFO)
    elif sha != KEVIN_REFERENCE["input_sha256"]:
        c.add(f"source_row {KNOWN_ROW}", "not applicable (different input)",
              "only checked on the reference HI-Small_Trans.csv", INFO)
    elif KNOWN_ROW not in sub.index:
        c.ok(f"source_row {KNOWN_ROW}", False, "missing", "present in full mode")
    else:
        row = sub.loc[KNOWN_ROW]
        got = (str(row["from_acct"]), str(row["to_acct"]), iso(row["ts"]), float(row["amt_paid"]),
               str(row["ccy_paid"]), str(row["fmt"]), int(row["label"]), bool(row["self_transfer"]))
        want = ("48309_811C599A0", "48309_811C599A0", "2022-09-04T05:03:00", 48649.42,
                "Saudi Riyal", "ACH", 1, True)
        c.ok(f"source_row {KNOWN_ROW} as expected", got == want, str(got), str(want))
    return spot


def recompute_rates(train):
    """Own FX fit: median of USD ratios per currency (training rows only)."""
    paid = text_values(train["ccy_paid"])
    recv = text_values(train["ccy_recv"])
    a_paid = train["amt_paid"].to_numpy()
    a_recv = train["amt_recv"].to_numpy()
    positive = (a_paid > 0) & (a_recv > 0)
    ratios = {}
    m1 = positive & (paid != USD) & (recv == USD)
    for ccy, r in zip(paid[m1], a_recv[m1] / a_paid[m1]):
        ratios.setdefault(ccy, []).append(r)
    m2 = positive & (paid == USD) & (recv != USD)
    for ccy, r in zip(recv[m2], a_paid[m2] / a_recv[m2]):
        ratios.setdefault(ccy, []).append(r)
    return {ccy: (float(np.median(vals)), len(vals)) for ccy, vals in ratios.items()}


def check_fx(c, df, splits, fx):
    c.start("Exchange rates (FX)")
    cutoff = splits["validation_start"]
    c.ok("fit_cutoff_exclusive == validation_start", fx.get("fit_cutoff_exclusive") == cutoff,
         fx.get("fit_cutoff_exclusive"), cutoff)
    train_mask = df["ts"].to_numpy() < np.datetime64(pd.Timestamp(cutoff))
    split_train = text_values(df["split"]) == "train"
    c.ok("Training rows are exactly ts < cutoff",
         np.array_equal(train_mask, split_train) and int(train_mask.sum()) == fx.get("training_rows"),
         f"{int(train_mask.sum()):,} rows (fx_rates.json: {fx.get('training_rows')})", "same rows, same count")

    ours = recompute_rates(df[train_mask])
    file_rates = fx["rates"]
    used = set(text_values(df["ccy_paid"])) | set(text_values(df["ccy_recv"]))
    missing = sorted(used - set(file_rates))
    c.ok("Every currency in the data has a rate", not missing, "missing: " + (", ".join(missing) or "none"),
         "no missing rates")
    c.ok("US Dollar fixed at 1.0", file_rates.get(USD, {}).get("rate") == 1.0,
         file_rates.get(USD, {}).get("rate"), "1.0")

    bad = []
    rows = []
    for ccy in sorted(set(file_rates) - {USD}):
        f_rate, f_n = file_rates[ccy]["rate"], file_rates[ccy]["n_obs"]
        o_rate, o_n = ours.get(ccy, (None, 0))
        rel = abs(o_rate - f_rate) / abs(f_rate) if o_rate is not None else math.inf
        if rel > FX_RELATIVE_TOLERANCE or o_n != f_n:
            bad.append(ccy)
        rows.append([ccy, f"{f_rate:.12g}", f"{o_rate:.12g}" if o_rate is not None else "none",
                     f"{rel:.1e}", f_n, o_n])
    extra_ccy = sorted(set(ours) - set(file_rates))
    c.ok("Rates re-computed from train rows", not bad and not extra_ccy,
         f"{len(rows)} currencies, mismatched: {bad or 'none'}, extra: {extra_ccy or 'none'}",
         f"relative difference <= {FX_RELATIVE_TOLERANCE:g} and same n_obs")
    c.extra(md_table(["Currency", "fx_rates.json", "re-computed", "rel. diff", "n_obs file", "n_obs ours"],
                     rows))

    rate_map = {k: v["rate"] for k, v in file_rates.items()}
    rate = np.array([rate_map.get(x, np.nan) for x in text_values(df["ccy_paid"])], dtype="float64")
    expected = df["amt_paid"].to_numpy() * rate
    usd = df["amount_usd"].to_numpy()
    c.ok("amount_usd == amt_paid * rate[ccy_paid]", bool(np.array_equal(usd, expected)),
         f"{int((usd != expected).sum())} rows differ", "exact equality")
    c.ok("amount_usd finite", bool(np.isfinite(usd).all()), f"{int((~np.isfinite(usd)).sum())} not finite",
         "no NaN / inf")
    return ours


def check_reference_rates(c, path, fx, mode):
    c.start("Reference rates file")
    with open(path, "r", encoding="utf-8") as f:
        ref = json.load(f)
    ours = {k: v["rate"] for k, v in fx["rates"].items()}
    rows = []
    bad = []
    for ccy in sorted(set(ref) | set(ours)):
        o, r = ours.get(ccy), ref.get(ccy)
        if o is None or r is None:
            bad.append(ccy)
            rows.append([ccy, o, r, "missing", "-"])
            continue
        diff = abs(o - r)
        within = diff <= REFERENCE_ABS_TOLERANCE
        if not within:
            bad.append(ccy)
        rows.append([ccy, f"{o:.12g}", repr(r), f"{diff:.2e}", "yes" if within else "no"])
    rule = f"|ours - reference| <= {REFERENCE_ABS_TOLERANCE:g} per currency ({path})"
    measured = f"{len(rows)} currencies, outside tolerance: {bad or 'none'}"
    if mode == "full":
        c.ok("Rates match reference", not bad, measured, rule)
    else:
        c.add("Rates vs reference (sample rates are expected to differ)", measured, rule, INFO)
    c.extra(md_table(["Currency", "Ours", "Reference", "Abs. difference", "Within tolerance"], rows))


def own_measurements(df, fx):
    """The quantities Kevin reported, re-measured from the clean table."""
    paid = text_values(df["ccy_paid"])
    recv = text_values(df["ccy_recv"])
    label = df["label"].to_numpy()
    late = df["ts"].to_numpy() >= np.datetime64("2022-09-11T00:00")
    st = df["self_transfer"].to_numpy()
    dup = df.duplicated(subset=list(DUPLICATE_KEY), keep=False)
    dup_groups = int(df.loc[dup, list(DUPLICATE_KEY)].drop_duplicates().shape[0])
    non_usd = {k: v for k, v in fx["rates"].items() if k != USD}
    return {
        "rows": len(df),
        "positives": int((label == 1).sum()),
        "negatives": int((label == 0).sum()),
        "ts_min": iso(df["ts"].min()),
        "ts_max": iso(df["ts"].max()),
        "rows_after_2022_09_10": int(late.sum()),
        "positives_after_2022_09_10": int((late & (label == 1)).sum()),
        "self_transfers": int(st.sum()),
        "positive_self_transfers": int((st & (label == 1)).sum()),
        "duplicate_groups": dup_groups,
        "duplicate_rows": int(dup.sum()),
        "currencies": len(set(paid) | set(recv)),
        "payment_formats": len(set(text_values(df["fmt"]))),
        "cross_currency_rows": int((paid != recv).sum()),
        "fx_training_observations": int(sum(v["n_obs"] for v in non_usd.values())),
        "fx_non_usd_currencies": len(non_usd),
    }


def check_kevin(c, df, splits, fx, mode, sha):
    c.start("Kevin's reference values")
    ref = KEVIN_REFERENCE
    if mode != "full":
        c.add("Kevin's reference", "not applicable (sample run)", "full mode only", INFO)
        return
    if sha != ref["input_sha256"]:
        c.add("Kevin's reference", "not applicable (different input)", f"input sha256 {ref['input_sha256']}",
              INFO)
        return
    got = own_measurements(df, fx)
    for key, value in got.items():
        c.ok(key, value == ref[key], value, f"Kevin: {ref[key]}")
    c.ok("validation_start", splits["validation_start"] == ref["validation_start"],
         splits["validation_start"], f"Kevin: {ref['validation_start']}")
    c.ok("test_start", splits["test_start"] == ref["test_start"], splits["test_start"],
         f"Kevin: {ref['test_start']}")
    for name, want in ref["splits"].items():
        have = {k: splits["summary"][name][k] for k in want}
        c.ok(f"{name} split", have == want, have, f"Kevin: {want}")
    tol = ref["rate_relative_tolerance"]
    for ccy, (rate, n_obs) in ref["rates"].items():
        mine = fx["rates"].get(ccy)
        if mine is None:
            c.ok(f"rate {ccy}", False, "missing", f"Kevin: {rate} ({n_obs})")
            continue
        rel = abs(mine["rate"] - rate) / rate
        c.ok(f"rate {ccy}", rel <= tol and mine["n_obs"] == n_obs,
             f"{mine['rate']:.12g} ({mine['n_obs']}), rel. diff {rel:.1e}",
             f"Kevin: {rate} ({n_obs}); rel. tol {tol:g}, n_obs exact")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def render(c, mode, out_dir):
    statuses = c.statuses()
    n_fail = statuses.count(FAIL)
    overall = FAIL if n_fail else PASS
    lines = ["# Validation report (independent checker)", ""]
    if mode != "full":
        lines += ["> **SAMPLE RUN — results describe only the sampled rows**", ""]
    lines += [f"**Overall status: {overall}** ({statuses.count(PASS)} PASS, {n_fail} FAIL, "
              f"{statuses.count(INFO)} INFO)", "", f"Checked folder: `{out_dir}`", ""]
    for title, checks, extra in c.sections:
        lines += [f"## {title}", "",
                  md_table(["Check", "Measured", "Rule / expectation", "Status"],
                           [[x["name"], x["measured"], x["rule"], x["status"]] for x in checks]), ""]
        if extra:
            lines += [extra.rstrip(), ""]
    return "\n".join(lines), overall


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Independently validate the cleaning outputs.")
    parser.add_argument("--out", required=True, help="folder with the cleaning outputs")
    parser.add_argument("--input", required=True, help="the raw CSV that was cleaned")
    parser.add_argument("--reference-rates", help="JSON {currency: rate} rounded to 6 decimals")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)

    for name in ("splits.json", "fx_rates.json"):
        if not (out_dir / name).exists():
            print(f"[validate] FAIL: {out_dir / name} not found (did the cleaning run fail?)",
                  file=sys.stderr)
            return 1
    with open(out_dir / "splits.json", "r", encoding="utf-8") as f:
        splits = json.load(f)
    with open(out_dir / "fx_rates.json", "r", encoding="utf-8") as f:
        fx = json.load(f)
    prov = splits["provenance"]
    mode, nrows, sample_every = prov["mode"], prov["nrows"], prov["sample_every"]
    clean_csv = out_dir / ("clean.csv" if mode == "full" else "clean_sample.csv")
    print(f"[validate] reading {clean_csv}", flush=True)
    df = read_clean_csv(clean_csv)

    c = Checks()
    c.start("Inputs")
    c.add("Mode", mode, f"nrows={nrows}, sample_every={sample_every}", INFO)
    c.ok("fx_rates.json provenance == splits.json provenance", fx.get("provenance") == prov,
         "equal" if fx.get("provenance") == prov else "different", "same run")
    print("[validate] hashing input", flush=True)
    sha = file_sha256(args.input)
    c.ok("Input SHA-256 equals provenance", sha == prov["input_sha256"], sha, prov["input_sha256"])

    check_schema(c, df)
    check_ids_and_order(c, df, mode, nrows, sample_every)
    check_splits(c, df, splits)
    print("[validate] scanning raw CSV", flush=True)
    check_raw(c, df, args.input, mode, nrows, sample_every, sha)
    print("[validate] checking FX", flush=True)
    check_fx(c, df, splits, fx)
    if args.reference_rates:
        check_reference_rates(c, Path(args.reference_rates).expanduser(), fx, mode)
    check_kevin(c, df, splits, fx, mode, sha)

    text, overall = render(c, mode, out_dir)
    with open(out_dir / "validation.md", "w", encoding="utf-8") as f:
        f.write(text)
    fails = [x["name"] for _, checks, _ in c.sections for x in checks if x["status"] == FAIL]
    print(f"[validate] overall status: {overall}" + (f" — FAIL: {fails}" if fails else ""))
    print(f"[validate] report: {out_dir / 'validation.md'}")
    return 1 if overall == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
