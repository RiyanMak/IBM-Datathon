"""Offline experiment: do "taint" clues (the model's own scores on money that arrived
earlier) improve XGBoost? Nothing here is wired into the live scorer.

Usage (from the repo root):
  python -m scripts.train_taint --data data/processed --features-file data/processed/features_39.csv \\
      --out data/models_taint

The idea: the simulator labels laundering transitively, so money forwarded from a
suspicious arrival is itself laundering. Feed the scores of transfers that reached an
account in the last 24 hours back in as clues for the account's next transfers.

Leakage rules, both enforced here:
  - a transfer's taint clues use only scores of transfers that came before it (earlier
    rows of the time-sorted table);
  - every score used comes from a model that never trained on the scored row. Stage one
    is cross-fitted: the training period is cut in two halves by time, a model trained on
    each half scores the other. Validation and test rows are scored by a stage-one model
    trained on the whole training period (the same one the scorer would run live).

Stage two is XGBoost on the clues plus five taint clues, trained exactly like the scorer
(early stopping on validation PR-AUC, thresholds chosen on validation). The same seeds are
also trained without the taint clues, so the test table compares like with like.

Taint clues, per transfer from sender S to receiver R (0 when nothing arrived in 24 h):
  snd_taint_max    highest stage-one score among transfers into S in the last 24 h
  snd_taint_mean   their mean score
  snd_taint_share  amount-weighted mean score: the share of S's recent inflow that looks dirty
  rcv_taint_max    the same two for transfers into R
  rcv_taint_mean

Two known imperfections, both small: the half models are trained on half the data, so their
scores on the training rows are a little less confident than the full model's on validation
and test; and a half model that scores a transfer just before the cut has trained on rows
just after it, so taint clues within 24 hours after the cut carry a faint echo of their labels.
"""

import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from common.features import DAY, FEATURE_NAMES
from scripts.train_xgboost import (BASE_PARAMS, TS_FORMAT, choose_thresholds, format_table, predict,
                                   split_tables, to_markdown, train_config)

TAINT_NAMES = ["snd_taint_max", "snd_taint_mean", "snd_taint_share", "rcv_taint_max", "rcv_taint_mean"]


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- taint memory

class _Inflow:
    """The last 24 h of transfers into one account: (minute, score, amount), with running
    sums for the means and a decreasing deque for the sliding maximum."""
    __slots__ = ("items", "peaks", "n", "sum_score", "sum_amt", "sum_amt_score")

    def __init__(self):
        self.items = deque()
        self.peaks = deque()       # (minute, score), scores strictly decreasing from the left
        self.n = 0
        self.sum_score = 0.0
        self.sum_amt = 0.0
        self.sum_amt_score = 0.0

    def prune(self, now):
        cutoff = now - DAY
        items = self.items
        while items and items[0][0] < cutoff:
            _, s, a = items.popleft()
            self.n -= 1
            self.sum_score -= s
            self.sum_amt -= a
            self.sum_amt_score -= a * s
        while self.peaks and self.peaks[0][0] < cutoff:
            self.peaks.popleft()
        if not self.n:             # an empty window is exactly zero, whatever the float residue
            self.sum_score = self.sum_amt = self.sum_amt_score = 0.0

    def add(self, t, score, amount):
        self.items.append((t, score, amount))
        self.n += 1
        self.sum_score += score
        self.sum_amt += amount
        self.sum_amt_score += amount * score
        while self.peaks and self.peaks[-1][1] <= score:
            self.peaks.pop()
        self.peaks.append((t, score))

    def stats(self):
        if not self.n:
            return 0.0, 0.0, 0.0
        share = self.sum_amt_score / self.sum_amt if self.sum_amt > 0 else self.sum_score / self.n
        return self.peaks[0][1], self.sum_score / self.n, min(max(share, 0.0), 1.0)


def taint_clues(ts_min, src, dst, amount, score, progress=1_000_000):
    """Walk the rows in time order; each row's taint clues come from earlier rows only,
    then its own score joins the receiver's inflow. Returns an (n, 5) float32 array."""
    n = len(ts_min)
    out = np.zeros((n, len(TAINT_NAMES)), dtype=np.float32)
    inflow = {}
    t0 = time.time()
    for i in range(n):
        t = int(ts_min[i])
        s = inflow.get(src[i])
        if s is not None:
            s.prune(t)
            mx, mean, share = s.stats()
            out[i, 0], out[i, 1], out[i, 2] = mx, mean, share
        r = inflow.get(dst[i])
        if r is None:
            r = inflow[dst[i]] = _Inflow()
        else:
            r.prune(t)
            mx, mean, _ = r.stats()
            out[i, 3], out[i, 4] = mx, mean
        r.add(t, float(score[i]), max(float(amount[i]), 0.0))
        if progress and i % progress == 0 and i:
            log("taint: %s rows, %.0f s" % (f"{i:,}", time.time() - t0))
    return out


# ---------------------------------------------------------------- stage one

def stage_one_scores(X, y, split, ts_min, columns, nthread, seed=0):
    """Out-of-sample stage-one scores for every row (see the module docstring)."""
    in_train = np.flatnonzero(split == "train")
    in_val = split == "validation"
    t = ts_min[in_train]
    cut = t[len(t) // 2]
    half_a, half_b = in_train[t < cut], in_train[t >= cut]
    log("stage one: half A %s rows (%d laundering), half B %s rows (%d), cut at minute %d"
        % (f"{len(half_a):,}", y[half_a].sum(), f"{len(half_b):,}", y[half_b].sum(), cut))
    X_val, y_val = X[in_val], y[in_val]
    scores = np.zeros(len(y), dtype=np.float32)
    for fit, other, name in ((half_a, half_b, "A"), (half_b, half_a, "B")):
        run = train_config("stage one on half %s" % name, BASE_PARAMS, X[fit], y[fit], X_val, y_val,
                           columns, [seed], nthread)
        scores[other] = predict(run["boosters"][0], X[other], columns)
    full = train_config("stage one on all of train", BASE_PARAMS, X[in_train], y[in_train], X_val, y_val,
                        columns, [seed], nthread)
    rest = np.flatnonzero(split != "train")
    scores[rest] = predict(full["boosters"][0], X[rest], columns)
    return scores


# ---------------------------------------------------------------- command line

def main(argv=None):
    p = argparse.ArgumentParser(description="Offline taint-clue experiment (cross-fitted stage one, stage two)")
    p.add_argument("--data", required=True, help="folder with clean.csv")
    p.add_argument("--features-file", help="clue table (default: <data>/features.csv)")
    p.add_argument("--out", required=True, help="folder for taint_report.md and taint_report.json")
    p.add_argument("--seeds", default="0,1,2", help="stage-two seeds (default: %(default)s)")
    p.add_argument("--nthread", type=int, default=os.cpu_count() or 1)
    args = p.parse_args(argv)
    data, out = Path(args.data), Path(args.out)
    features_path = Path(args.features_file) if args.features_file else data / "features.csv"
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    t_start = time.time()

    head = list(pd.read_csv(features_path, nrows=0).columns)
    columns = [c for c in FEATURE_NAMES if c in head]
    t0 = time.time()
    feat = pd.read_csv(features_path, usecols=["txn_id", "split", "label", "amount_usd", *columns],
                       dtype={**{c: "float32" for c in columns}, "split": str, "label": "int8"})
    acct = pd.read_csv(data / "clean.csv", usecols=["txn_id", "ts", "from_acct", "to_acct"],
                       dtype={"from_acct": str, "to_acct": str}, na_filter=False)
    if not np.array_equal(acct.txn_id.to_numpy(), feat.txn_id.to_numpy()):
        acct = acct.set_index("txn_id").reindex(feat.txn_id).reset_index()
    ts = pd.to_datetime(acct.ts, format=TS_FORMAT).to_numpy()
    ts_min = ts.astype("datetime64[m]").astype("int64")
    if np.any(np.diff(ts_min) < 0):
        raise SystemExit("rows are not in time order")
    log("loaded %s rows x %d clues in %.0f s" % (f"{len(feat):,}", len(columns), time.time() - t0))

    X = feat[columns].to_numpy(dtype=np.float32)
    y = feat.label.to_numpy().astype(int)
    split = feat.split.to_numpy()
    amount = feat.amount_usd.to_numpy()
    del feat

    scores = stage_one_scores(X, y, split, ts_min, columns, args.nthread)
    t0 = time.time()
    T = taint_clues(ts_min, acct.from_acct.to_numpy(), acct.to_acct.to_numpy(), amount, scores)
    log("taint clues in %.0f s" % (time.time() - t0))
    del acct

    m_train, m_val, m_test = split == "train", split == "validation", split == "test"
    means = pd.DataFrame(T, columns=TAINT_NAMES).groupby(y).mean().T
    means.columns = ["normal", "laundering"]
    log("taint clue means by label (all rows):\n" + means.round(4).to_string())

    both = np.concatenate([X, T], axis=1)
    both_cols = [*columns, *TAINT_NAMES]
    del T
    runs = {}
    for name, M, cols in (("without taint", X, columns), ("with taint", both, both_cols)):
        runs[name] = (train_config(name, BASE_PARAMS, M[m_train], y[m_train], M[m_val], y[m_val], cols, seeds,
                                   args.nthread), M, cols)

    entries, validation_rows, gains = [], {}, {}
    y_test, ts_test, amt_test = y[m_test], ts[m_test], amount[m_test]
    for name, (run, M, cols) in runs.items():
        X_test = M[m_test]
        p_seeds = [predict(b, X_test, cols) for b in run["boosters"]]
        for seed, p, pv, ap in zip(run["seeds"], p_seeds, run["p_val"], run["val_ap"]):
            entries.append((f"{name}, seed {seed}", p, choose_thresholds(y[m_val], pv)["alert"]))
            validation_rows[f"{name}, seed {seed}"] = ap
        entries.append((f"{name}, average of {len(seeds)} seeds", np.mean(p_seeds, axis=0),
                        choose_thresholds(y[m_val], run["p_val_avg"])["alert"]))
        validation_rows[f"{name}, average of {len(seeds)} seeds"] = run["val_ap_avg"]
        g = run["boosters"][0].get_score(importance_type="gain")
        gains[name] = {c: float(g.get(c, 0.0)) for c in cols}
    tables = split_tables(entries, y_test, ts_test, amt_test)

    report = {"features_file": str(features_path), "clues": columns, "taint_clues": TAINT_NAMES, "seeds": seeds,
              "validation_pr_auc": validation_rows,
              "taint_means_by_label": means.to_dict(),
              "test": {part: df.T.to_dict() for part, df in tables.items()},
              "gain_with_taint_seed0": gains["with taint"], "minutes": round((time.time() - t_start) / 60, 1)}
    titles = {"overall": "Test, all rows", "before": "Test, before 2022-09-11", "after": "Test, from 2022-09-11 on"}
    md = ["# Taint clue experiment (offline only)", "",
          "Clues: %d from `%s` plus %d taint clues. Stage one cross-fitted on two halves of train by time; "
          "validation and test scored by the stage-one model trained on all of train." % (
              len(columns), features_path, len(TAINT_NAMES)), "",
          "## Validation PR-AUC", "", "| model | validation PR-AUC |", "|---|---|"]
    md += ["| %s | %.4f |" % (k, v) for k, v in validation_rows.items()]
    for part in ("overall", "before", "after"):
        md += ["", "## " + titles[part], "", to_markdown(tables[part])]
    md += ["", "## Taint clue means by label", "", "| clue | normal | laundering |", "|---|---|---|"]
    md += ["| %s | %.4f | %.4f |" % (c, r["normal"], r["laundering"]) for c, r in means.iterrows()]
    rank = sorted(gains["with taint"].items(), key=lambda kv: -kv[1])
    md += ["", "## Gain rank of the taint clues (with taint, seed %d)" % seeds[0], ""]
    md += ["- %s: rank %d of %d" % (c, i + 1, len(rank)) for i, (c, _) in enumerate(rank) if c in TAINT_NAMES]
    out.mkdir(parents=True, exist_ok=True)
    (out / "taint_report.md").write_text("\n".join(md) + "\n")
    (out / "taint_report.json").write_text(json.dumps(report, indent=2, default=float))
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print("\nvalidation PR-AUC")
        for k, v in validation_rows.items():
            print("  %-45s %.4f" % (k, v))
        for part in ("overall", "before", "after"):
            print("\n" + titles[part])
            print(format_table(tables[part]).to_string())
    log("wrote %s/taint_report.md (%.1f min)" % (out, report["minutes"]))
    return report


if __name__ == "__main__":
    main()
