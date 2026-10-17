"""Clean HI-Small_Trans.csv into one Parquet table plus JSON/Markdown reports.

Usage (from the repo root):

    python -m scripts.clean_data --input HI-Small_Trans.csv --out data/processed
    python -m scripts.clean_data --input HI-Small_Trans.csv --out data/processed/sample --sample-every 50
    python -m scripts.clean_data --input HI-Small_Trans.csv --out data/processed/sample_head --nrows 5000

Outputs in --out:
    clean.parquet (full mode) or clean_sample.parquet (sample modes)
    splits.json, fx_rates.json, data_quality.md

If the data breaks the contract (bad values, missing exchange rates, splits
that cannot be made) the run stops, writes data_quality.md with status FAILED
and the details, writes no parquet/JSON, and exits with code 1.
Exit code 1 is also returned if a check in data_quality.md is FAIL (the files
are then still written so they can be inspected). Exit code 2 means a usage
problem (e.g. outputs already exist).
"""

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from common.clean_transactions import (
    DataValidationError,
    load_raw,
    normalize,
    sort_and_assign_ids,
)
from common.data_contract import CLEAN_COLUMN_ORDER
from common.fx import MissingRateError, apply_rates, fit_rates, fx_consistency
from common.quality import render_failure_report, render_report
from common.splits import assign_split, build_splits_json, compute_cutoffs, split_summary, to_iso

SCHEMA_VERSION = "v1"
REPORT_NAME = "data_quality.md"
SPLITS_NAME = "splits.json"
FX_NAME = "fx_rates.json"
CHUNK_SIZE = 1024 * 1024  # 1 MB


class StageFailure(Exception):
    """A data problem stopped the run at a named stage."""

    def __init__(self, stage, error):
        self.stage = stage
        self.error = error
        super().__init__(f"{stage}: {error}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def parquet_name(mode) -> str:
    return "clean.parquet" if mode == "full" else "clean_sample.parquet"


def output_names(mode) -> list:
    return [parquet_name(mode), SPLITS_NAME, FX_NAME, REPORT_NAME]


def sha256_of_file(path) -> str:
    """SHA-256 of a file, read in 1 MB chunks (the file is never fully loaded)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def peak_memory_bytes() -> int:
    """Peak resident memory of this process so far, or 0 if unknown.

    The `resource` module only exists on Linux and macOS. ru_maxrss is in
    bytes on macOS but in KiB on Linux. On Windows the report says 0 bytes.
    """
    try:
        import resource
    except ImportError:  # Windows
        return 0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss) if sys.platform == "darwin" else int(rss) * 1024


class StageTimer:
    """Measures the runtime of each stage and prints one line per stage."""

    def __init__(self):
        self.timings = {}

    @contextmanager
    def stage(self, name):
        print(f"[clean_data] {name} ...", flush=True)
        start = time.perf_counter()
        try:
            yield
        except BaseException:
            self.timings[name] = time.perf_counter() - start
            print(f"[clean_data] {name} FAILED after {self.timings[name]:.2f}s", flush=True)
            raise
        self.timings[name] = time.perf_counter() - start
        mib = peak_memory_bytes() / 2**20
        print(f"[clean_data] {name} done in {self.timings[name]:.2f}s (peak memory {mib:,.0f} MiB)",
              flush=True)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, allow_nan=False)
        f.write("\n")


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def tmp_path_for(final_path: Path) -> Path:
    """Temporary name in the same folder, so the final rename is atomic."""
    return final_path.with_name(f".{final_path.name}.tmp")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def build_provenance(input_path, mode, nrows, sample_every) -> dict:
    return {
        "input_path": str(input_path),
        "input_bytes": os.path.getsize(input_path),
        "input_sha256": sha256_of_file(input_path),
        "mode": mode,
        "nrows": nrows,
        "sample_every": sample_every,
        "provisional_ids": mode != "full",
        "schema_version": SCHEMA_VERSION,
        # Run metadata only (when the run happened), not part of the data.
        "run_started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def run_steps(input_path, nrows, sample_every, provenance, timer) -> dict:
    """Load, clean, sort, split and convert. Raises StageFailure on data problems."""
    with timer.stage("load_raw"):
        try:
            raw = load_raw(input_path, nrows=nrows, sample_every=sample_every)
        except DataValidationError as e:
            raise StageFailure("load_raw", e)

    with timer.stage("normalize"):
        try:
            df = normalize(raw)
        except DataValidationError as e:
            raise StageFailure("normalize", e)
    del raw  # the all-text frame is large and no longer needed

    with timer.stage("sort_and_assign_ids"):
        try:
            df = sort_and_assign_ids(df)
        except ValueError as e:
            raise StageFailure("sort_and_assign_ids", e)

    with timer.stage("splits"):
        try:
            cutoffs = compute_cutoffs(df["ts"])
        except ValueError as e:
            raise StageFailure("splits", e)
        df["split"] = assign_split(df["ts"], cutoffs)

    with timer.stage("fx_fit"):
        all_currencies = (set(df["ccy_paid"].astype(object).unique())
                          | set(df["ccy_recv"].astype(object).unique()))
        train = df[df["split"] == "train"]
        try:
            fit = fit_rates(train, all_currencies)
        except MissingRateError as e:
            raise StageFailure("fx_fit", e)
        training_rows = len(train)
        del train

    with timer.stage("fx_apply"):
        try:
            df["amount_usd"] = apply_rates(df, fit)
        except (MissingRateError, ValueError) as e:
            raise StageFailure("fx_apply", e)

    with timer.stage("fx_consistency"):
        consistency = fx_consistency(df, fit)

    with timer.stage("build_outputs"):
        clean = df[list(CLEAN_COLUMN_ORDER)]
        del df
        splits_json = build_splits_json(cutoffs, split_summary(clean), provenance)
        fx_json = dict(fit)
        fx_json["fit_cutoff_exclusive"] = to_iso(cutoffs["validation_start"])
        fx_json["training_rows"] = int(training_rows)
        fx_json["provenance"] = dict(provenance)

    return {"clean": clean, "splits_json": splits_json, "fx_json": fx_json,
            "consistency": consistency}


def reload_check(path, frame) -> dict:
    """Read the parquet back and compare it with the in-memory frame."""
    back = pd.read_parquet(path)
    mismatches = [c for c in frame.columns
                  if c not in back.columns or back[c].dtype != frame[c].dtype]
    if list(back.columns) != list(frame.columns):
        mismatches.append("<column list differs>")
    return {
        "equals": bool(back.equals(frame)),
        "dtypes_identical": not mismatches,
        "dtype_mismatches": mismatches,
        "file_bytes": os.path.getsize(path),
    }


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Clean the IBM AML transaction CSV.")
    parser.add_argument("--input", required=True, help="path to HI-Small_Trans.csv")
    parser.add_argument("--out", required=True, help="output folder")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--nrows", type=int, help="head sample: only the first N data rows")
    group.add_argument("--sample-every", type=int, help="stride sample: every K-th data row")
    parser.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    args = parser.parse_args(argv)
    if args.nrows is not None and args.nrows < 1:
        parser.error("--nrows must be at least 1")
    if args.sample_every is not None and args.sample_every < 1:
        parser.error("--sample-every must be at least 1")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.nrows is not None:
        mode = "head_sample"
    elif args.sample_every is not None:
        mode = "stride_sample"
    else:
        mode = "full"

    input_path = Path(args.input)
    out_dir = Path(args.out)
    if not input_path.is_file():
        print(f"[clean_data] input file not found: {input_path}", file=sys.stderr)
        return 2

    names = output_names(mode)
    existing = [n for n in names if (out_dir / n).exists()]
    if existing and not args.overwrite:
        print(f"[clean_data] refusing to overwrite existing output(s) in {out_dir}: "
              f"{', '.join(existing)} (use --overwrite)", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    timer = StageTimer()
    with timer.stage("provenance_sha256"):
        provenance = build_provenance(args.input, mode, args.nrows, args.sample_every)

    final = {n: out_dir / n for n in names}
    tmp = {n: tmp_path_for(p) for n, p in final.items()}
    try:
        try:
            result = run_steps(input_path, args.nrows, args.sample_every, provenance, timer)
        except StageFailure as failure:
            # With --overwrite, old outputs from an earlier run must not sit
            # next to a FAILED report, so remove them.
            for n in names:
                if n != REPORT_NAME and final[n].exists():
                    final[n].unlink()
            text = render_failure_report(failure.stage, failure.error, provenance,
                                         timer.timings, peak_memory_bytes())
            write_text(tmp[REPORT_NAME], text)
            os.replace(tmp[REPORT_NAME], final[REPORT_NAME])
            print(f"[clean_data] FAILED at stage {failure.stage}: {failure.error}", file=sys.stderr)
            print(f"[clean_data] report: {final[REPORT_NAME]}", file=sys.stderr)
            return 1

        clean = result["clean"]
        pq = parquet_name(mode)
        with timer.stage("write_parquet"):
            clean.to_parquet(tmp[pq], index=False)
        with timer.stage("reload_check"):
            reload_result = reload_check(tmp[pq], clean)
        with timer.stage("write_json"):
            write_json(tmp[SPLITS_NAME], result["splits_json"])
            write_json(tmp[FX_NAME], result["fx_json"])
        with timer.stage("quality_report"):
            text = render_report(clean, provenance, result["splits_json"], result["fx_json"],
                                 result["consistency"], reload_result, timer.timings,
                                 peak_memory_bytes())
            write_text(tmp[REPORT_NAME], text)

        # Every step succeeded: move all files into place.
        for n in names:
            os.replace(tmp[n], final[n])
    finally:
        for p in tmp.values():
            if p.exists():
                p.unlink()

    status_line = next(line for line in text.splitlines() if line.startswith("**Overall status"))
    print(f"[clean_data] {status_line.replace('**', '')}")
    print(f"[clean_data] wrote {', '.join(str(final[n]) for n in names)}")
    # A FAIL check in the report (e.g. the parquet reload check) is not a
    # silent success: the files are kept for inspection but the exit code is 1.
    return 1 if status_line.startswith("**Overall status: FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
