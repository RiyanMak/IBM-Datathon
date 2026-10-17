"""Measurements on the cleaned transactions, and the data_quality.md report.

Every function here only READS the frames and dicts it is given; nothing is
written to disk and no data is changed.

A "check" is a small dict:

    {"name": str, "measured": str, "rule_or_expectation": str, "status": str}

status is "PASS" or "FAIL" only when there is a concrete rule to test.
Plain measurements without a rule get "INFO".

Typical use (see scripts/clean_data.py):

    text = render_report(clean, provenance, splits_json, fx_json, consistency,
                         reload_result, timings, peak_memory_bytes)
"""

import platform

import numpy as np
import pandas as pd

try:
    import pyarrow
    PYARROW_VERSION = pyarrow.__version__
except ImportError:  # not available on the LinuxONE image; only reported, never required
    PYARROW_VERSION = "not installed"

from common.clean_transactions import DataValidationError
from common.data_contract import CLEAN_COLUMN_ORDER, SPLIT_NAMES, USD
from common.fx import MissingRateError
from common.splits import to_iso

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"

# The dataset description says transactions run until 2022-09-10; rows after
# this minute are measured (and kept).
LATE_AFTER = pd.Timestamp("2022-09-10 23:59")

# The 11 fields of the raw file, as clean columns. Two rows that agree on all
# of them are exact duplicates of each other in the source.
SOURCE_FIELDS = (
    "ts",
    "from_bank_raw",
    "from_acct_raw",
    "to_bank_raw",
    "to_acct_raw",
    "amt_recv",
    "ccy_recv",
    "amt_paid",
    "ccy_paid",
    "fmt",
    "label",
)

MAX_EXAMPLES = 10

SAMPLE_BANNER = "> **SAMPLE RUN — results describe only the sampled rows**"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def make_check(name, measured, rule, status):
    """Build one check dict."""
    if status not in (PASS, FAIL, INFO):
        raise ValueError(f"unknown status {status!r}")
    return {"name": name, "measured": str(measured), "rule_or_expectation": rule, "status": status}


def pass_fail(ok) -> str:
    return PASS if ok else FAIL


def fmt_int(n) -> str:
    return f"{int(n):,}"


def fmt_rate(x) -> str:
    """A float with 12 significant digits (enough to compare with references)."""
    return "None" if x is None else f"{x:.12g}"


def md_table(header, rows) -> str:
    """Render a Markdown table. `rows` is a list of lists of values."""
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cell(h) for h in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def checks_table(checks) -> str:
    rows = [[c["name"], c["measured"], c["rule_or_expectation"], c["status"]] for c in checks]
    return md_table(["Check", "Measured", "Rule / expectation", "Status"], rows)


def _text(s: pd.Series) -> pd.Series:
    """Column as plain Python text (so categoricals compare as strings)."""
    return s.astype(object)


def section(title, checks, extra=""):
    """One report section: a checks table plus optional extra Markdown."""
    return {"title": title, "checks": list(checks), "extra": extra}


# ---------------------------------------------------------------------------
# Measurements (one function per report section)
# ---------------------------------------------------------------------------

def input_section(provenance, rows_read):
    p = provenance
    if p["mode"] == "head_sample":
        mode_text = f"head_sample (first {fmt_int(p['nrows'])} data rows)"
    elif p["mode"] == "stride_sample":
        mode_text = f"stride_sample (every {p['sample_every']}th data row)"
    else:
        mode_text = "full"
    checks = [
        make_check("Input path", p["input_path"], "as given on the command line", INFO),
        make_check("Input size", f"{fmt_int(p['input_bytes'])} bytes", "measured", INFO),
        make_check("Input SHA-256", p["input_sha256"], "streamed in 1 MB chunks", INFO),
        make_check("Mode", mode_text, "chosen on the command line", INFO),
        make_check("Rows read", fmt_int(rows_read), "rows kept by the mode", INFO),
        make_check("IDs provisional", p["provisional_ids"],
                   "true in sample modes (txn_id only valid within the sample)", INFO),
        make_check("Run started", p["run_started_at"], "wall clock, local time", INFO),
    ]
    return section("Input identity", checks)


def validation_section(problems):
    n = len(problems)
    checks = [make_check("Validation problems", n, "0 problems in the raw data contract", pass_fail(n == 0))]
    return section("Raw data validation", checks)


def labels_section(df):
    n = len(df)
    positives = int((df["label"] == 1).sum())
    negatives = int((df["label"] == 0).sum())
    rate = positives / n if n else 0.0
    checks = [
        make_check("Rows", fmt_int(n), "measured", INFO),
        make_check("Positives (label 1)", fmt_int(positives), "measured", INFO),
        make_check("Negatives (label 0)", fmt_int(negatives), "measured", INFO),
        make_check("Positive rate", f"{rate:.6%}", "measured", INFO),
        make_check("Labels only 0 or 1", f"{fmt_int(positives + negatives)} of {fmt_int(n)}",
                   "positives + negatives == rows", pass_fail(positives + negatives == n)),
    ]
    return section("Rows and labels", checks)


def dates_section(df):
    late = df["ts"] > LATE_AFTER
    late_pos = int((late & (df["label"] == 1)).sum())
    checks = [
        make_check("Earliest ts", to_iso(df["ts"].min()), "measured", INFO),
        make_check("Latest ts", to_iso(df["ts"].max()), "measured", INFO),
        make_check("Rows after 2022-09-10 23:59", fmt_int(late.sum()),
                   "measured; these rows are kept", INFO),
        make_check("Positives after 2022-09-10 23:59", fmt_int(late_pos),
                   "measured; these rows are kept", INFO),
    ]
    return section("Date range", checks)


def self_transfer_section(df):
    st = df["self_transfer"]
    checks = [
        make_check("Self-transfers", fmt_int(st.sum()), "measured; kept, label unchanged", INFO),
        make_check("Positive self-transfers", fmt_int((st & (df["label"] == 1)).sum()),
                   "measured; kept, label unchanged", INFO),
    ]
    return section("Self-transfers", checks)


def duplicates_section(df):
    """Rows identical on all 11 source fields (amounts compared as floats)."""
    dup_mask = df.duplicated(subset=list(SOURCE_FIELDS), keep=False)
    dups = df.loc[dup_mask, list(SOURCE_FIELDS) + ["source_row"]]
    groups = dups.groupby(list(SOURCE_FIELDS), observed=True, sort=False)["source_row"].apply(list)
    n_groups = len(groups)
    examples = [sorted(int(r) for r in rows) for rows in groups.head(MAX_EXAMPLES)]
    checks = [
        make_check("Duplicate groups", fmt_int(n_groups), "measured; all rows kept", INFO),
        make_check("Rows in duplicate groups", fmt_int(dup_mask.sum()), "measured; all rows kept", INFO),
    ]
    extra = ""
    if examples:
        extra = "Example groups (source_row values): " + "; ".join(str(e) for e in examples)
    return section("Exact duplicate rows", checks, extra)


def amounts_section(df):
    zero_paid = int((df["amt_paid"] == 0).sum())
    zero_recv = int((df["amt_recv"] == 0).sum())
    checks = [
        make_check("Rows with amt_paid == 0", fmt_int(zero_paid), "measured; kept", INFO),
        make_check("Rows with amt_recv == 0", fmt_int(zero_recv), "measured; kept", INFO),
    ]
    # Amounts per currency, both sides together.
    both = pd.concat([
        pd.DataFrame({"currency": _text(df["ccy_paid"]), "amount": df["amt_paid"]}),
        pd.DataFrame({"currency": _text(df["ccy_recv"]), "amount": df["amt_recv"]}),
    ], ignore_index=True)
    stats = both.groupby("currency")["amount"].agg(["count", "min", "max"]).sort_index()
    rows = [[c, fmt_int(r["count"]), f"{r['min']:.10g}", f"{r['max']:.10g}"] for c, r in stats.iterrows()]
    extra = "Native amounts per currency (paid and received sides together):\n\n" + md_table(
        ["Currency", "Amounts (both sides)", "Min", "Max"], rows)
    return section("Amounts", checks, extra)


def categories_section(df):
    paid = _text(df["ccy_paid"])
    recv = _text(df["ccy_recv"])
    currencies = sorted(set(paid) | set(recv))
    cross = paid != recv
    no_usd = cross & (paid != USD) & (recv != USD)
    formats = _text(df["fmt"]).value_counts().sort_index()
    checks = [
        make_check("Distinct currencies", len(currencies), "measured (both sides)", INFO),
        make_check("Distinct payment formats", len(formats), "measured", INFO),
        make_check("Cross-currency rows", fmt_int(cross.sum()), "ccy_paid != ccy_recv", INFO),
        make_check("Cross-currency rows with no USD side", fmt_int(no_usd.sum()),
                   "not usable for FX fitting", INFO),
    ]
    paid_counts = paid.value_counts()
    recv_counts = recv.value_counts()
    ccy_rows = [[c, fmt_int(paid_counts.get(c, 0)), fmt_int(recv_counts.get(c, 0))] for c in currencies]
    fmt_rows = [[f, fmt_int(n)] for f, n in formats.items()]
    extra = (md_table(["Currency", "Rows paid in", "Rows received in"], ccy_rows)
             + "\n\n" + md_table(["Payment format", "Rows"], fmt_rows))
    return section("Currencies and payment formats", checks, extra)


def bank_spellings(df):
    """{canonical bank: sorted list of raw spellings} for banks with >1 spelling."""
    pairs = pd.concat([
        pd.DataFrame({"bank": _text(df["from_bank"]), "raw": _text(df["from_bank_raw"])}),
        pd.DataFrame({"bank": _text(df["to_bank"]), "raw": _text(df["to_bank_raw"])}),
    ], ignore_index=True).drop_duplicates()
    spellings = pairs.groupby("bank")["raw"].apply(lambda s: sorted(s))
    return {b: raws for b, raws in spellings.items() if len(raws) > 1}


def banks_section(df):
    multi = bank_spellings(df)
    examples = [f"{b}: {raws}" for b, raws in sorted(multi.items())[:MAX_EXAMPLES]]
    checks = [make_check("Canonical banks with >1 raw spelling", fmt_int(len(multi)),
                         "measured; leading zeros removed, see limitations", INFO)]
    extra = ("Examples: " + "; ".join(examples)) if examples else ""
    return section("Bank code spellings", checks, extra), len(multi)


def ids_section(df):
    n = len(df)
    txn_ok = bool(np.array_equal(df["txn_id"].to_numpy(), np.arange(n, dtype="int64")))
    sr_unique = not df["source_row"].duplicated().any()
    cols_ok = tuple(df.columns) == CLEAN_COLUMN_ORDER
    checks = [
        make_check("txn_id contiguous", f"0..{n - 1}" if txn_ok else "not contiguous",
                   "txn_id == 0..N-1 in row order", pass_fail(txn_ok)),
        make_check("source_row unique", sr_unique, "no duplicate source_row", pass_fail(sr_unique)),
        make_check("Column order", f"{len(df.columns)} columns",
                   "exactly CLEAN_COLUMN_ORDER", pass_fail(cols_ok)),
    ]
    return section("Identifiers", checks)


def ordering_section(df):
    ts = df["ts"].to_numpy()
    sr = df["source_row"].to_numpy()
    same_ts = ts[1:] == ts[:-1]
    ok = bool(np.all(ts[1:] >= ts[:-1]) and np.all(~same_ts | (sr[1:] > sr[:-1])))
    checks = [make_check("Sorted by (ts, source_row)", ok, "ascending, ties by source_row", pass_fail(ok))]
    return section("Ordering", checks)


def splits_section(df, splits_json):
    v_start = pd.Timestamp(splits_json["validation_start"])
    t_start = pd.Timestamp(splits_json["test_start"])
    ts = df["ts"]
    expected = np.select([ts < v_start, ts < t_start], ["train", "validation"], default="test")
    rules_ok = bool(np.array_equal(_text(df["split"]).to_numpy(), expected))
    splits_per_ts = df.groupby("ts", observed=True)["split"].nunique()
    shared = int((splits_per_ts > 1).sum())
    summary = splits_json["summary"]
    total = sum(summary[name]["rows"] for name in SPLIT_NAMES)

    checks = [
        make_check("validation_start", splits_json["validation_start"], splits_json["method"], INFO),
        make_check("test_start", splits_json["test_start"], splits_json["method"], INFO),
        make_check("Every row follows the split rules", rules_ok,
                   "; ".join(splits_json["rules"].values()), pass_fail(rules_ok)),
        make_check("Timestamps in more than one split", shared, "0", pass_fail(shared == 0)),
        make_check("Split rows add up to N", f"{fmt_int(total)} of {fmt_int(len(df))}",
                   "train + validation + test == N", pass_fail(total == len(df))),
    ]
    rows = []
    for name in SPLIT_NAMES:
        s = summary[name]
        share = s["rows"] / len(df) if len(df) else 0.0
        rows.append([name, fmt_int(s["rows"]), f"{share:.2%}", fmt_int(s["positives"]),
                     f"{s['txn_id_min']}..{s['txn_id_max']}", f"{s['ts_min']} .. {s['ts_max']}"])
    extra = md_table(["Split", "Rows", "Share", "Positives", "txn_id range", "ts range"], rows)
    return section("Chronological splits", checks, extra)


def _describe_text(d):
    if d["count"] == 0:
        return "no rows"
    return (f"count {fmt_int(d['count'])}, median {d['median']:.4g}, p90 {d['p90']:.4g}, "
            f"p99 {d['p99']:.4g}, max {d['max']:.4g}")


def fx_section(df, fx_json, consistency):
    rates = fx_json["rates"]
    used = set(_text(df["ccy_paid"])) | set(_text(df["ccy_recv"]))
    missing = sorted(used - set(rates))
    finite = bool(np.isfinite(df["amount_usd"].to_numpy()).all())
    checks = [
        make_check("Fit cutoff (exclusive)", fx_json["fit_cutoff_exclusive"],
                   "rates learned only from rows with ts < validation_start", INFO),
        make_check("Training rows used", fmt_int(fx_json["training_rows"]), "train split rows", INFO),
        make_check("Total FX observations", fmt_int(fx_json["total_observations"]), "measured", INFO),
        make_check("FX warnings", len(fx_json["warnings"]),
                   "; ".join(fx_json["warnings"]) or "none", INFO),
        make_check("Every currency in the data has a rate", "missing: " + (", ".join(missing) or "none"),
                   "no missing rates", pass_fail(not missing)),
        make_check("All amount_usd finite", finite, "no NaN / inf", pass_fail(finite)),
        make_check("Consistency (all rows)", _describe_text(consistency["all_rows"]),
                   "diagnostic: |amt_recv*rate_recv - amount_usd| / amount_usd", INFO),
        make_check("Consistency (cross-currency rows)", _describe_text(consistency["cross_currency_rows"]),
                   "diagnostic only", INFO),
        make_check("Consistency rows skipped",
                   f"no recv rate {consistency['skipped_no_recv_rate']}, "
                   f"zero amount_usd {consistency['skipped_zero_amount_usd']}", "diagnostic only", INFO),
    ]
    rows = []
    for c in sorted(rates):
        r = rates[c]
        if r.get("fixed"):
            rows.append([c, fmt_rate(r["rate"]), "fixed", "", "", "", "", ""])
            continue
        rows.append([c, fmt_rate(r["rate"]), fmt_int(r["n_obs"]), fmt_int(r["n_paid_ccy_recv_usd"]),
                     fmt_int(r["n_paid_usd_recv_ccy"]), fmt_rate(r["p01"]), fmt_rate(r["p99"]),
                     r["low_support"]])
    extra = (f"Rates are {fx_json['unit']}.\n\n" + md_table(
        ["Currency", "Rate", "n_obs", "paid C / recv USD", "paid USD / recv C", "p01", "p99", "low_support"],
        rows))
    return section("Exchange rates (FX)", checks, extra)


def reload_section(reload_result):
    r = reload_result
    checks = [
        make_check("CSV reload equals in-memory frame", r["equals"],
                   "read_clean_csv(file).equals(frame)", pass_fail(r["equals"])),
        make_check("CSV reload dtypes identical", r["dtypes_identical"]
                   if not r["dtype_mismatches"] else f"False: {r['dtype_mismatches']}",
                   "every column dtype restored", pass_fail(r["dtypes_identical"])),
        make_check("CSV file size", f"{fmt_int(r['file_bytes'])} bytes", "measured", INFO),
    ]
    return section("CSV reload check", checks)


def environment_info():
    return {
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "pyarrow": PYARROW_VERSION,
        "platform": platform.platform(),
    }


def environment_section(timings, peak_memory_bytes):
    env = environment_info()
    checks = [make_check(k, v, "measured", INFO) for k, v in env.items()]
    checks.append(make_check("Peak memory", f"{peak_memory_bytes / 2**20:,.0f} MiB",
                             "ru_maxrss of this process", INFO))
    rows = [[name, f"{seconds:.2f}"] for name, seconds in timings.items()]
    rows.append(["total", f"{sum(timings.values()):.2f}"])
    extra = "Runtime per stage:\n\n" + md_table(["Stage", "Seconds"], rows)
    return section("Environment and runtime", checks, extra)


def limitations(mode, multi_spelling_banks):
    items = [
        "The raw timestamps have no timezone; they are kept as naive local times.",
        "Rows with the same minute are ordered by their position in the raw file (source_row). "
        "This is a convention, not the true order of events.",
        "Exchange rates are estimates learned from this synthetic data (training rows only); "
        "they are not real historical market rates.",
        "Exact duplicate rows, self-transfers, zero amounts and rows after 2022-09-10 are kept "
        "and only measured; nothing is auto-fixed.",
    ]
    if multi_spelling_banks > 0:
        items.append(f"{multi_spelling_banks} canonical bank code(s) appear with more than one raw "
                     "spelling (leading zeros). They are treated as the same bank; the raw spelling "
                     "is kept in from_bank_raw / to_bank_raw.")
    if mode != "full":
        items.append("SAMPLE ONLY — not full-dataset results; IDs provisional; splits/rates not final.")
    return items


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def collect_sections(clean, provenance, splits_json, fx_json, consistency,
                     reload_result, timings, peak_memory_bytes, problems=()):
    """All sections of a successful run, plus the limitations list."""
    banks, n_multi = banks_section(clean)
    sections = [
        input_section(provenance, len(clean)),
        validation_section(list(problems)),
        labels_section(clean),
        dates_section(clean),
        self_transfer_section(clean),
        duplicates_section(clean),
        amounts_section(clean),
        categories_section(clean),
        banks,
        ids_section(clean),
        ordering_section(clean),
        splits_section(clean, splits_json),
        fx_section(clean, fx_json, consistency),
        reload_section(reload_result),
        environment_section(timings, peak_memory_bytes),
    ]
    return sections, limitations(provenance["mode"], n_multi)


def overall_status(sections):
    statuses = [c["status"] for s in sections for c in s["checks"]]
    n_fail = statuses.count(FAIL)
    status = FAIL if n_fail else PASS
    return status, statuses.count(PASS), n_fail, statuses.count(INFO)


def render_report(clean, provenance, splits_json, fx_json, consistency,
                  reload_result, timings, peak_memory_bytes, problems=()):
    """Markdown text of data_quality.md for a run that finished."""
    sections, limits = collect_sections(clean, provenance, splits_json, fx_json, consistency,
                                        reload_result, timings, peak_memory_bytes, problems)
    status, n_pass, n_fail, n_info = overall_status(sections)
    lines = ["# Data quality report", ""]
    if provenance["mode"] != "full":
        lines += [SAMPLE_BANNER, ""]
    lines += [f"**Overall status: {status}** ({n_pass} PASS, {n_fail} FAIL, {n_info} INFO)", ""]
    for s in sections:
        lines += [f"## {s['title']}", "", checks_table(s["checks"]), ""]
        if s["extra"]:
            lines += [s["extra"], ""]
    lines += ["## Limitations", ""] + [f"- {item}" for item in limits] + [""]
    return "\n".join(lines)


def _failure_details(error):
    """Markdown lines describing why a stage failed."""
    lines = []
    if isinstance(error, DataValidationError):
        lines.append(f"{len(error.problems)} data problem(s) found:")
        lines.append("")
        for p in error.problems:
            lines.append(f"### {p['check']} in column `{p['column']}` — {fmt_int(p['count'])} row(s)")
            lines.append("")
            rows = [[ex["source_row"], ex["raw_value"]] for ex in p["examples"]]
            lines.append(md_table(["source_row", "raw value"], rows))
            lines.append("")
    elif isinstance(error, MissingRateError):
        lines.append("No exchange rate could be learned from the training rows for: "
                     + ", ".join(error.currencies))
        lines.append("")
        rows = [[c, fmt_int(error.row_counts[c]) if c in error.row_counts else "?"] for c in error.currencies]
        lines.append(md_table(["Currency", "Row count (meaning given in the message below)"], rows))
        lines.append("")
        lines.append(f"Message: `{error}`")
        lines.append("")
    else:
        lines.append(f"{type(error).__name__}: `{error}`")
        lines.append("")
    return lines


def render_failure_report(stage, error, provenance, timings, peak_memory_bytes):
    """Markdown text of data_quality.md for a run that stopped on a data problem."""
    lines = ["# Data quality report", ""]
    if provenance["mode"] != "full":
        lines += [SAMPLE_BANNER, ""]
    lines += [
        "**Overall status: FAILED**",
        "",
        f"The run stopped at stage **{stage}**. No CSV or JSON output was written. "
        "Nothing was auto-fixed: fix the input or the code and run again.",
        "",
        "## Failure details",
        "",
    ]
    lines += _failure_details(error)
    inp = input_section(provenance, rows_read=0)
    # "Rows read" is not known for a failed run, so leave it out.
    inp["checks"] = [c for c in inp["checks"] if c["name"] != "Rows read"]
    env = environment_section(timings, peak_memory_bytes)
    for s in (inp, env):
        lines += [f"## {s['title']}", "", checks_table(s["checks"]), ""]
        if s["extra"]:
            lines += [s["extra"], ""]
    return "\n".join(lines)
