"""Load, validate and type the raw IBM AML transaction file (HI-Small_Trans.csv).

Typical use:

    raw = load_raw("HI-Small_Trans.csv")
    clean = normalize(raw)   # raises DataValidationError if anything is wrong

load_raw and normalize never drop or reorder rows. sort_and_assign_ids sorts
the rows by time and adds txn_id. FX conversion and splits live in fx.py and
splits.py.
"""

import csv
import re

import numpy as np
import pandas as pd

from common.data_contract import (
    CLEAN_NAMES,
    RAW_COLUMNS,
    TS_FORMAT,
    make_account_id_series,
    canonical_bank_series,
)

# String dtype used for text columns in the normalized frame.
# The pyarrow-backed string dtype stores ~5M rows of text in compact Arrow
# buffers instead of one Python object per cell, so it needs far less memory.
# pyarrow is not available on the LinuxONE (s390x) image, so fall back to the
# python-backed dtype there; the values and behaviour are the same.
try:
    import pyarrow  # noqa: F401
    STRING_DTYPE = pd.StringDtype("pyarrow")
except ImportError:
    STRING_DTYPE = pd.StringDtype("python")

# Columns of the clean table grouped by dtype, so clean.csv can be read back
# with exactly the types normalize() produced (a CSV keeps no types itself).
_CSV_STRING_COLUMNS = ("from_bank", "from_acct_raw", "from_acct", "to_bank", "to_acct_raw", "to_acct")
# Category columns are built from plain str text (pandas' default), as normalize() does.
_CSV_CATEGORY_COLUMNS = ("from_bank_raw", "to_bank_raw", "ccy_recv", "ccy_paid", "fmt")
_CSV_NUMERIC_DTYPES = {"txn_id": "int64", "source_row": "int64", "amt_recv": "float64",
                       "amt_paid": "float64", "amount_usd": "float64", "label": "int8",
                       "self_transfer": "bool"}

# Maximum number of example rows stored for each problem.
MAX_EXAMPLES = 20

# Regular expressions for the validation checks. We write [0-9] instead of \d
# on purpose: in Python, \d also matches non-ASCII digits.
_BANK_RE = r"[0-9]+"
_TS_RE = r"[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}"
_AMOUNT_RE = r"[0-9]+(\.[0-9]+)?"
_WHITESPACE_RE = r"\s"

_BANK_COLUMNS = ("from_bank", "to_bank")
_ACCOUNT_COLUMNS = ("from_acct_raw", "to_acct_raw")
_AMOUNT_COLUMNS = ("amt_recv", "amt_paid")


class DataValidationError(Exception):
    """Raised when the raw data breaks the data contract.

    `problems` is a list of dicts:
        {"check": str, "column": str, "count": int,
         "examples": [{"source_row": int, "raw_value": str}, ...]}
    """

    def __init__(self, problems):
        self.problems = list(problems)
        lines = [f"{len(self.problems)} data problem(s) found:"]
        for p in self.problems:
            rows = [ex["source_row"] for ex in p["examples"]]
            lines.append(
                f"  - {p['check']} in column '{p['column']}': {p['count']} row(s), "
                f"e.g. source_row {rows}"
            )
        super().__init__("\n".join(lines))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def read_header(path) -> list:
    """Return the header (first line) of the CSV file as a list of names."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        return next(reader, [])


def _is_kept(source_row, sample_every):
    """True if a data row with this source_row is kept when sampling."""
    return sample_every is None or source_row % sample_every == 0


def _scan_field_counts(path, nrows, sample_every):
    """Count the fields of every kept data row with the csv module.

    pandas fills missing trailing fields of a short row with "" when
    na_filter=False, so a short row would look like a row with empty cells.
    This scan tells them apart. It returns two dicts {source_row: n_fields}:
    one for rows with too few fields and one for rows with too many.
    """
    expected = len(RAW_COLUMNS)
    too_few = {}
    too_many = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip the header
        for source_row, row in enumerate(reader):
            if nrows is not None and source_row >= nrows:
                break
            if not _is_kept(source_row, sample_every):
                continue
            if len(row) < expected:
                too_few[source_row] = len(row)
            elif len(row) > expected:
                too_many[source_row] = len(row)
    return too_few, too_many


def load_raw(path, nrows=None, sample_every=None) -> pd.DataFrame:
    """Read the raw CSV with every value kept as the exact original text.

    Columns: source_row, then CLEAN_NAMES (renamed by position).
    source_row is the zero-based position of the data row in the raw file
    (the first row after the header is 0), also when sampling.

    nrows:        only read the first N data rows.
    sample_every: only keep data rows whose source_row % k == 0.
    """
    if nrows is not None and sample_every is not None:
        raise ValueError("use either nrows or sample_every, not both")
    if sample_every is not None and (not isinstance(sample_every, int) or sample_every < 1):
        raise ValueError("sample_every must be a positive integer")
    if nrows is not None and (not isinstance(nrows, int) or nrows < 0):
        raise ValueError("nrows must be a non-negative integer")

    # 1) The header must match exactly.
    header = read_header(path)
    if tuple(header) != RAW_COLUMNS:
        raise DataValidationError([{
            "check": "header",
            "column": "<header>",
            "count": 1,
            "examples": [{"source_row": -1, "raw_value": ",".join(header)}],
        }])

    # 2) Find rows with the wrong number of fields.
    too_few, too_many = _scan_field_counts(path, nrows, sample_every)
    if too_many:
        rows = sorted(too_many)
        raise DataValidationError([{
            "check": "too_many_fields",
            "column": "<row>",
            "count": len(rows),
            "examples": [
                {"source_row": r, "raw_value": f"{too_many[r]} fields"}
                for r in rows[:MAX_EXAMPLES]
            ],
        }])

    # 3) Decide which raw lines to skip. Line 0 is the header, so the data
    #    row with source_row r is on line r + 1.
    if sample_every is None:
        skiprows = [0]
    else:
        def skiprows(line_number):
            if line_number == 0:
                return True
            return not _is_kept(line_number - 1, sample_every)

    # 4) Read everything as text. Nothing is converted, trimmed or turned
    #    into NaN ("NA", "NULL", "" stay literal strings).
    df = pd.read_csv(
        path,
        header=None,
        skiprows=skiprows,
        names=list(CLEAN_NAMES),
        nrows=nrows,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        skip_blank_lines=False,  # a blank line is a (broken) row; keep it so positions stay right
        on_bad_lines="error",
        engine="c",
        encoding="utf-8",
    )

    # 5) source_row: the kept rows are 0, k, 2k, ... (or 0, 1, 2, ...).
    step = 1 if sample_every is None else sample_every
    source_row = np.arange(len(df), dtype="int64") * step
    df.insert(0, "source_row", source_row)

    # 6) Mark the cells that are really missing in short rows as missing,
    #    so validate_raw reports them as "missing_field".
    for row_number, n_fields in too_few.items():
        positions = np.flatnonzero(df["source_row"].to_numpy() == row_number)
        for col in CLEAN_NAMES[n_fields:]:
            df.iloc[positions, df.columns.get_loc(col)] = None

    return df


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _as_python_text(s: pd.Series) -> pd.Series:
    """Object-dtype copy of a text column.

    String methods on object dtype use Python's own `re` and `str.strip`,
    so the results match the scalar helpers in data_contract exactly.
    """
    return s.astype(object)


def _fullmatch(s: pd.Series, pattern: str) -> pd.Series:
    """Boolean mask: True where the whole value matches the pattern."""
    return _as_python_text(s).str.fullmatch(pattern).fillna(False).astype(bool)


def _make_problem(df, bad_mask, check, column):
    """Build one problem dict from a boolean mask of bad rows."""
    bad = df.loc[bad_mask, ["source_row", column]].head(MAX_EXAMPLES)
    examples = [
        {"source_row": int(r), "raw_value": "<missing>" if pd.isna(v) else str(v)}
        for r, v in zip(bad["source_row"], bad[column])
    ]
    return {"check": check, "column": column, "count": int(bad_mask.sum()), "examples": examples}


def validate_raw(df: pd.DataFrame) -> list:
    """Check the raw text frame from load_raw. Returns a list of problems.

    Cells that are missing are reported once as "missing_field" and are not
    checked again by the other checks.
    """
    problems = []

    def add(bad_mask, check, column):
        if bad_mask.any():
            problems.append(_make_problem(df, bad_mask, check, column))

    missing = {}
    for col in CLEAN_NAMES:
        missing[col] = df[col].isna()
        add(missing[col], "missing_field", col)

    # Bank codes: ASCII digits only after removing outer whitespace.
    for col in _BANK_COLUMNS:
        stripped = _as_python_text(df[col]).str.strip()
        bad = ~missing[col] & ~_fullmatch(stripped, _BANK_RE)
        add(bad, "bank_code", col)

    # Accounts: not empty and no whitespace anywhere.
    for col in _ACCOUNT_COLUMNS:
        text = _as_python_text(df[col])
        has_space = text.str.contains(_WHITESPACE_RE, regex=True).fillna(False).astype(bool)
        bad = ~missing[col] & ((text == "") | has_space)
        add(bad, "account", col)

    # Timestamp: right shape AND a real date/time (2022/02/30 is rejected).
    shape_ok = _fullmatch(df["ts"], _TS_RE)
    parsed = pd.to_datetime(df["ts"].where(shape_ok), format=TS_FORMAT, errors="coerce")
    bad = ~missing["ts"] & (~shape_ok | parsed.isna())
    add(bad, "timestamp", "ts")

    # Amounts: plain non-negative decimal text, finite after parsing
    # (rejects "-5", "inf", "nan", "1e5", "").
    for col in _AMOUNT_COLUMNS:
        shape_ok = _fullmatch(df[col], _AMOUNT_RE)
        values = pd.to_numeric(df[col].where(shape_ok), errors="coerce").astype("float64")
        value_ok = np.isfinite(values) & (values >= 0)
        bad = ~missing[col] & (~shape_ok | ~value_ok)
        add(bad, "amount", col)

    # Label: exactly "0" or "1".
    bad = ~missing["label"] & ~df["label"].isin(["0", "1"])
    add(bad, "label", "label")

    return problems


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Validate the raw frame and return a NEW frame with proper types.

    Raises DataValidationError if validate_raw finds any problem.
    Row order is unchanged and no rows are dropped.
    """
    problems = validate_raw(df)
    if problems:
        raise DataValidationError(problems)

    out = pd.DataFrame(index=df.index)
    out["source_row"] = df["source_row"].astype("int64")

    # Timezone-naive timestamps in nanosecond resolution.
    out["ts"] = pd.to_datetime(df["ts"], format=TS_FORMAT).astype("datetime64[ns]")

    for side in ("from", "to"):
        bank_raw = df[f"{side}_bank"]
        acct_raw = df[f"{side}_acct_raw"]
        out[f"{side}_bank"] = canonical_bank_series(bank_raw).astype(STRING_DTYPE)
        out[f"{side}_bank_raw"] = bank_raw.astype("category")
        out[f"{side}_acct_raw"] = acct_raw.astype(STRING_DTYPE)
        out[f"{side}_acct"] = make_account_id_series(bank_raw, acct_raw).astype(STRING_DTYPE)

    out["amt_recv"] = pd.to_numeric(df["amt_recv"]).astype("float64")
    out["ccy_recv"] = df["ccy_recv"].astype("category")
    out["amt_paid"] = pd.to_numeric(df["amt_paid"]).astype("float64")
    out["ccy_paid"] = df["ccy_paid"].astype("category")
    out["fmt"] = df["fmt"].astype("category")
    out["label"] = df["label"].astype("int8")

    # A self-transfer is only flagged; its label is never changed.
    out["self_transfer"] = (out["from_acct"] == out["to_acct"]).astype(bool)

    return out


TS_CSV_FORMAT = "%Y-%m-%dT%H:%M:%S"


def write_clean_csv(df: pd.DataFrame, path) -> None:
    """Write the clean table as CSV with every timestamp in one ISO format.

    Without an explicit date_format pandas decides per chunk (a few thousand
    rows) whether to write "2022-09-01" or "2022-09-01 00:20:00", so a chunk of
    all-midnight rows (the start of HI-Small) produces a file with two formats.
    """
    df.to_csv(path, index=False, date_format=TS_CSV_FORMAT)


def read_clean_csv(path, columns=None) -> pd.DataFrame:
    """Read clean.csv (as written by scripts/clean_data.py) back with the same
    dtypes as the in-memory frame: int64 ids, datetime64[ns] ts, string accounts,
    category currencies/formats/split, float64 amounts, int8 label, bool flag.

    Text is read with na_filter=False so accounts such as "NA" stay text.
    Columns missing from the file (e.g. before splits are added) are skipped.
    columns: read only these (in this order), leaving the rest on disk; every
    one must be in the file.
    """
    present = set(pd.read_csv(path, nrows=0).columns)
    if columns is not None:
        missing = [c for c in columns if c not in present]
        if missing:
            raise ValueError(f"{path} has no column(s) {missing}")
        present = set(columns)
    dtype = {c: STRING_DTYPE for c in _CSV_STRING_COLUMNS if c in present}
    dtype.update({c: str for c in (*_CSV_CATEGORY_COLUMNS, "split") if c in present})
    dtype.update({c: t for c, t in _CSV_NUMERIC_DTYPES.items() if c in present})
    # round_trip: the default float parser can be off by one ulp on some values;
    # this one reproduces the written floats exactly.
    df = pd.read_csv(path, usecols=None if columns is None else list(columns), dtype=dtype,
                     na_filter=False, float_precision="round_trip")
    if "ts" in present:
        # ISO8601 accepts "2022-09-01", "2022-09-01 00:20:00" and "2022-09-01T00:20:00"
        # alike, so a file written by plain to_csv still reads correctly.
        df["ts"] = pd.to_datetime(df["ts"], format="ISO8601").astype("datetime64[ns]")
    for c in _CSV_CATEGORY_COLUMNS:
        if c in present:
            df[c] = df[c].astype("category")
    if "split" in present:
        from common.splits import SPLIT_DTYPE
        df["split"] = df["split"].astype(SPLIT_DTYPE)
    return df if columns is None else df[list(columns)]


# ---------------------------------------------------------------------------
# Ordering and transaction IDs
# ---------------------------------------------------------------------------

def sort_and_assign_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Sort the normalized frame by time and give every row a txn_id.

    Rows are sorted globally by (ts, source_row), ascending, in ONE stable
    sort. Rows with the same timestamp keep their original file order.
    txn_id is 0..N-1 in the new order and becomes the first column.

    Raises ValueError if source_row has duplicates (then the order of tied
    rows would not be well defined). No rows are ever dropped.
    """
    if df["source_row"].duplicated().any():
        dupes = df.loc[df["source_row"].duplicated(keep=False), "source_row"]
        examples = sorted(dupes.unique().tolist())[:MAX_EXAMPLES]
        raise ValueError(f"source_row has duplicate values, e.g. {examples}")

    out = df.sort_values(["ts", "source_row"], kind="mergesort").reset_index(drop=True)
    out.insert(0, "txn_id", np.arange(len(out), dtype="int64"))
    return out
