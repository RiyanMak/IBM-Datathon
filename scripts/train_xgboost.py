"""Train the XGBoost scorer from the command line: the notebook's sections 2 to 5 as a script.

Usage (from the repo root):
  python -m scripts.train_xgboost --data data/processed --models data/models_candidate --features
  python -m scripts.train_xgboost --seeds 0,1,2 --tune --compare-models data/models
  python -m scripts.train_xgboost --compare-models data/models --no-train   # only score an old model

What it does:
  1. clues: with --features every clean.csv row goes through ClueBuilder in time order and
     the table is written to --features-file (default <data>/features.csv). Without it the
     existing file is reused, but only if its columns are exactly FEATURE_NAMES.
  2. one XGBoost model per seed with the notebook's settings, early stopping on validation
     PR-AUC. Every score uses the trees up to the best iteration, as the live scorer does.
     With --tune a second configuration (deeper, slower, larger leaves) is trained on the
     same seeds; both are reported. Every choice is made on validation before any test
     score is computed.
  3. thresholds on validation only, as the notebook picks them: alert = best F1, ring =
     lowest score with precision >= 0.25, never above alert.
  4. test tables for every seed and for the seeds' averaged probabilities: precision,
     recall and F1 at the alert threshold, average precision (PR-AUC), ROC-AUC, the
     confusion counts and precision among the top 100 / 500 / 1,000 scores; overall and
     split at 2022-09-11, after which normal traffic nearly stops and every score looks
     better than it is.
  5. writes scorer.json (the single model, over every configuration and seed, with the
     best validation PR-AUC: the scorer loads one model, not an average), feature_list.json,
     thresholds.json, training_report.md and training_report.json into --models.

--compare-models DIR scores the same test rows with an existing model folder (its own
feature_list.json and thresholds.json), so an old model and a new one are compared on
identical rows.

Memory: clues are read as float32 and only the needed columns are loaded.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from common.features import FEATURE_NAMES, ClueBuilder
from common.paths import DATA_DIR, MODELS_DIR

SPLIT_DATE = "2022-09-11T00:00:00"
TOP_K = (100, 500, 1000)
PROGRESS_EVERY = 500_000
RING_MIN_PRECISION = 0.25
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"   # explicit: pandas on s390x crashes when it has to guess

# the notebook's settings
BASE_PARAMS = dict(n_estimators=2000, learning_rate=0.05, max_depth=6, subsample=0.8, colsample_bytree=0.8,
                   min_child_weight=1)
TUNED_PARAMS = dict(BASE_PARAMS, max_depth=8, learning_rate=0.03, min_child_weight=5)
EARLY_STOPPING = 100


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- clues

def build_features(data_dir, out_path, progress=PROGRESS_EVERY):
    """Every clean.csv row through ClueBuilder in time order; writes the clue table to out_path."""
    from common.clean_transactions import read_clean_csv

    t0 = time.time()
    df = read_clean_csv(Path(data_dir) / "clean.csv",
                        columns=["txn_id", "ts", "from_acct", "to_acct", "ccy_paid", "ccy_recv",
                                 "amount_usd", "fmt", "label", "split"])
    log("clean.csv: %s rows in %.0f s" % (f"{len(df):,}", time.time() - t0))
    n = len(df)
    X = np.empty((n, len(FEATURE_NAMES)), dtype=np.float32)
    builder = ClueBuilder()
    ts_min = df.ts.values.astype("datetime64[m]").astype("int64")
    cols = zip(ts_min, df.from_acct.astype(str).values, df.to_acct.astype(str).values, df.amount_usd.values,
               df.ccy_paid.astype(str).values, df.ccy_recv.astype(str).values, df.fmt.astype(str).values)
    t0 = time.time()
    for i, (t, fa, ta, amt, cp, cr, f) in enumerate(cols):
        X[i] = builder.compute_row(t, fa, ta, amt, cp, cr, f)
        if progress and i % progress == 0 and i:
            log("clues: %s rows, %.0f s" % (f"{i:,}", time.time() - t0))
    del builder, cols
    log("clues: %s rows x %d in %.0f s" % (f"{n:,}", len(FEATURE_NAMES), time.time() - t0))

    feat = pd.DataFrame(X, columns=FEATURE_NAMES)
    feat.insert(0, "txn_id", df.txn_id.values)
    feat.insert(1, "split", df.split.astype(str).values)
    feat["amount_usd"] = df.amount_usd.values   # for the baseline only, never a clue
    feat["label"] = df.label.values
    if feat[FEATURE_NAMES].isna().any().any():
        raise SystemExit("NaN in the clues")
    t0 = time.time()
    feat.to_csv(out_path, index=False, chunksize=250_000)
    log("wrote %s in %.0f s" % (out_path, time.time() - t0))


def check_feature_file(path):
    """The clue columns of an existing features file; it must hold exactly FEATURE_NAMES."""
    cols = list(pd.read_csv(path, nrows=0).columns)
    clue_cols = [c for c in cols if c not in ("txn_id", "split", "amount_usd", "label")]
    if clue_cols != list(FEATURE_NAMES):
        raise SystemExit("%s has %d clue columns, common/features.py has %d (or the order differs). "
                         "Run with --features to rebuild it, or point --features-file at a matching table."
                         % (path, len(clue_cols), len(FEATURE_NAMES)))


def load_table(features_path, clean_path, columns):
    """The clue table (float32) plus the time of every test row, from clean.csv by txn_id."""
    t0 = time.time()
    feat = pd.read_csv(features_path, usecols=["txn_id", "split", "label", "amount_usd", *columns],
                       dtype={**{c: "float32" for c in columns}, "split": str, "label": "int8"})
    ts = pd.read_csv(clean_path, usecols=["txn_id", "ts"])
    if not np.array_equal(ts.txn_id.to_numpy(), feat.txn_id.to_numpy()):
        ts = ts.set_index("txn_id").reindex(feat.txn_id).reset_index()
    feat["ts"] = pd.to_datetime(ts.ts, format=TS_FORMAT)
    log("loaded %s rows x %d clues in %.0f s" % (f"{len(feat):,}", len(columns), time.time() - t0))
    return feat


# ---------------------------------------------------------------- training

def best_range(booster):
    """(0, best_iteration + 1): the trees early stopping chose, as services/scorer does."""
    from services.scorer.scorer import best_iteration_range
    return best_iteration_range(booster)


def predict(booster, X, columns):
    import xgboost as xgb
    return booster.predict(xgb.DMatrix(X, feature_names=list(columns)), iteration_range=best_range(booster))


def train_one(dtrain, dval, params, seed, spw, nthread):
    """One booster; xgb.train with the same settings XGBClassifier gets in the notebook."""
    import xgboost as xgb

    p = {"objective": "binary:logistic", "eval_metric": "aucpr", "tree_method": "hist",
         "eta": params["learning_rate"], "max_depth": params["max_depth"], "subsample": params["subsample"],
         "colsample_bytree": params["colsample_bytree"], "min_child_weight": params["min_child_weight"],
         "scale_pos_weight": spw, "seed": seed, "nthread": nthread}
    t0 = time.time()
    booster = xgb.train(p, dtrain, num_boost_round=params["n_estimators"], evals=[(dval, "validation")],
                        early_stopping_rounds=EARLY_STOPPING, verbose_eval=200)
    log("seed %d: best iteration %d, validation aucpr %.4f, %.0f s"
        % (seed, booster.best_iteration, booster.best_score, time.time() - t0))
    return booster


def train_config(name, params, X_train, y_train, X_val, y_val, columns, seeds, nthread):
    """One model per seed. Returns {"boosters", "p_val" per seed, "p_val_avg", validation APs}."""
    import xgboost as xgb
    from sklearn.metrics import average_precision_score

    spw = float((y_train == 0).sum() / (y_train == 1).sum())
    log("config %s: %s, scale_pos_weight %.1f, seeds %s" % (name, params, spw, list(seeds)))
    dtrain = xgb.QuantileDMatrix(X_train, label=y_train, feature_names=list(columns), nthread=nthread)
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=list(columns), nthread=nthread)
    boosters, p_val = [], []
    for seed in seeds:
        b = train_one(dtrain, dval, params, seed, spw, nthread)
        boosters.append(b)
        p_val.append(b.predict(dval, iteration_range=best_range(b)))
    del dtrain, dval
    p_avg = np.mean(p_val, axis=0)
    val_ap = [float(average_precision_score(y_val, p)) for p in p_val]
    return {"name": name, "params": params, "seeds": list(seeds), "boosters": boosters, "p_val": p_val,
            "p_val_avg": p_avg, "val_ap": val_ap, "val_ap_avg": float(average_precision_score(y_val, p_avg)),
            "best_iteration": [int(b.best_iteration) for b in boosters]}


# ---------------------------------------------------------------- thresholds and metrics

def choose_thresholds(y, p):
    """The notebook's rule, on validation only: alert = best F1, ring = lowest score with
    precision >= 0.25, never above alert."""
    from sklearn.metrics import precision_recall_curve

    prec, rec, thr = precision_recall_curve(y, p)
    prec, rec = prec[:-1], rec[:-1]           # drop the final (1, 0) point so arrays line up with thr
    f1 = 2 * prec * rec / (prec + rec + 1e-12)
    i_alert = int(np.argmax(f1))
    ok = np.where(prec >= RING_MIN_PRECISION)[0]
    i_ring = int(ok.min()) if len(ok) else i_alert
    return {
        "alert": float(thr[i_alert]),
        "ring": float(min(thr[i_ring], thr[i_alert])),
        "chosen_on": "validation",
        "val_precision_at_alert": float(prec[i_alert]),
        "val_recall_at_alert": float(rec[i_alert]),
        "val_f1_at_alert": float(f1[i_alert]),
        "val_precision_at_ring": float(prec[i_ring]),
        "val_recall_at_ring": float(rec[i_ring]),
    }


def metrics(y, p, threshold):
    """One table row: threshold metrics, ranking metrics, confusion counts and precision@K."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=np.float64)
    pred = p >= threshold
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    tn = int((~pred & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    out = {"rows": int(len(y)), "laundering": int(y.sum()), "precision": precision, "recall": recall,
           "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
           "pr_auc": float(average_precision_score(y, p)) if y.any() else float("nan"),
           "roc_auc": float(roc_auc_score(y, p)) if 0 < y.sum() < len(y) else float("nan"),
           "tp": tp, "fp": fp, "fn": fn, "tn": tn}
    order = np.argsort(-p, kind="stable")
    for k in TOP_K:
        top = order[:k]
        out[f"p@{k}"] = float(y[top].mean()) if len(top) == k else float("nan")
    return out


def split_tables(entries, y, ts, amount=None):
    """entries: [(row name, test probabilities, alert threshold)]. Returns {"overall", "before", "after"}
    DataFrames, the last two split at SPLIT_DATE."""
    cut = np.datetime64(SPLIT_DATE)
    masks = {"overall": np.ones(len(y), dtype=bool), "before": ts < cut, "after": ts >= cut}
    tables = {}
    for part, m in masks.items():
        rows = {name: metrics(y[m], p[m], thr) for name, p, thr in entries}
        if amount is not None and part == "overall":
            base = metrics(y[m], (amount[m] >= 10_000).astype(float), 0.5)
            for k in ("pr_auc", "roc_auc", *[f"p@{k}" for k in TOP_K]):
                base[k] = float("nan")     # a yes/no rule has no ranking
            rows["baseline >= $10k"] = base
        tables[part] = pd.DataFrame(rows).T
    return tables


COLUMNS_SHOWN = ["precision", "recall", "f1", "pr_auc", "roc_auc", "tp", "fp", "fn", "tn",
                 *[f"p@{k}" for k in TOP_K]]


def format_table(df):
    df = df[["rows", "laundering", *COLUMNS_SHOWN]].copy()
    for c in ("rows", "laundering", "tp", "fp", "fn", "tn"):
        df[c] = df[c].astype(int)
    return df.round(4)


def to_markdown(df):
    """A plain Markdown table (no tabulate dependency)."""
    df = format_table(df)
    head = "| | " + " | ".join(df.columns) + " |"
    sep = "|---|" + "---|" * len(df.columns)
    lines = [head, sep]
    for name, row in df.iterrows():
        cells = []
        for c in df.columns:
            v = row[c]
            cells.append("" if pd.isna(v) else (f"{int(v):,}" if c in ("rows", "laundering", "tp", "fp", "fn", "tn")
                                                 else f"{v:.4f}"))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- existing model folder

def load_existing(models_dir):
    """An existing model folder: (booster, feature list, thresholds)."""
    import xgboost as xgb

    models_dir = Path(models_dir)
    columns = json.loads((models_dir / "feature_list.json").read_text())
    unknown = [c for c in columns if c not in FEATURE_NAMES]
    if unknown:
        raise SystemExit("%s/feature_list.json names clues common/features.py does not compute: %s"
                         % (models_dir, unknown))
    thresholds = json.loads((models_dir / "thresholds.json").read_text())
    booster = xgb.Booster()
    booster.load_model(str(models_dir / "scorer.json"))
    return booster, columns, thresholds


# ---------------------------------------------------------------- command line

def build_parser():
    p = argparse.ArgumentParser(description="Train the XGBoost scorer (seeds, thresholds, test tables)")
    p.add_argument("--data", default=str(DATA_DIR), help="folder with clean.csv (default: %(default)s)")
    p.add_argument("--models", default=str(MODELS_DIR), help="output folder for the model files (default: %(default)s)")
    p.add_argument("--features-file", help="clue table to write or reuse (default: <data>/features.csv)")
    p.add_argument("--features", action="store_true", help="recompute the clue table from clean.csv")
    p.add_argument("--seeds", default="0,1,2", help="comma-separated seeds (default: %(default)s)")
    p.add_argument("--tune", action="store_true",
                   help="also train max_depth 8, learning_rate 0.03, min_child_weight 5; the saved model is "
                        "the best validation PR-AUC over both")
    p.add_argument("--compare-models", help="also score the test set with this existing model folder")
    p.add_argument("--no-train", action="store_true", help="skip training (with --compare-models: only score it)")
    p.add_argument("--nthread", type=int, default=os.cpu_count() or 1,
                   help="XGBoost threads (default: all cores, %(default)s)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    data = Path(args.data)
    out = Path(args.models)
    features_path = Path(args.features_file) if args.features_file else data / "features.csv"
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    if not seeds:
        raise SystemExit("--seeds needs at least one seed")
    t_start = time.time()

    if args.features:
        build_features(data, features_path)
    check_feature_file(features_path)

    compare = load_existing(args.compare_models) if args.compare_models else None
    columns = list(FEATURE_NAMES)
    feat = load_table(features_path, data / "clean.csv", columns)
    split = feat.split.to_numpy()
    m_train, m_val, m_test = split == "train", split == "validation", split == "test"
    X = feat[columns].to_numpy(dtype=np.float32)
    y = feat.label.to_numpy().astype(int)
    ts = feat.ts.to_numpy()
    amount = feat.amount_usd.to_numpy()
    del feat
    X_val, y_val = X[m_val], y[m_val]
    X_test, y_test, ts_test, amt_test = X[m_test], y[m_test], ts[m_test], amount[m_test]
    log("train %s (%d laundering), validation %s (%d), test %s (%d)" % (
        f"{m_train.sum():,}", y[m_train].sum(), f"{m_val.sum():,}", y_val.sum(), f"{m_test.sum():,}", y_test.sum()))

    report = {"features_file": str(features_path), "features": columns, "split_date": SPLIT_DATE,
              "test_rows": int(len(y_test)), "test_laundering": int(y_test.sum())}
    entries = []          # (name, test probabilities, alert threshold)
    validation_rows = {}  # name -> validation PR-AUC

    if compare is not None:
        booster, cols, thr = compare
        idx = [columns.index(c) for c in cols]
        name = "existing %d-clue model (%s)" % (len(cols), args.compare_models)
        from sklearn.metrics import average_precision_score
        validation_rows[name] = float(average_precision_score(y_val, predict(booster, X_val[:, idx], cols)))
        entries.append((name, predict(booster, X_test[:, idx], cols), float(thr["alert"])))
        report["compare"] = {"models": args.compare_models, "features": cols, "thresholds": thr,
                             "best_iteration_range": list(best_range(booster))}

    if not args.no_train:
        X_train, y_train = X[m_train], y[m_train]
        del X
        configs = [train_config("base", BASE_PARAMS, X_train, y_train, X_val, y_val, columns, seeds, args.nthread)]
        if args.tune:
            configs.append(train_config("tuned", TUNED_PARAMS, X_train, y_train, X_val, y_val, columns, seeds,
                                        args.nthread))
        del X_train, y_train
        # Every choice is made on validation only, before any test score is computed:
        #   - the configuration whose averaged seeds have the better validation PR-AUC;
        #   - the model the scorer loads: the single (configuration, seed) with the best
        #     validation PR-AUC, since the scorer runs one model, not an average.
        chosen = max(configs, key=lambda c: c["val_ap_avg"])
        saved_c, saved_i = max(((c, i) for c in configs for i in range(len(c["seeds"]))),
                               key=lambda ci: ci[0]["val_ap"][ci[1]])
        report["configs"] = [{k: c[k] for k in ("name", "params", "seeds", "val_ap", "val_ap_avg", "best_iteration")}
                             for c in configs]
        report["chosen_config"] = chosen["name"]
        for c in configs:
            for seed, ap in zip(c["seeds"], c["val_ap"]):
                validation_rows[f"{c['name']} seed {seed}"] = ap
            validation_rows[f"{c['name']} average of {len(c['seeds'])} seeds"] = c["val_ap_avg"]
        log("configuration with the better averaged validation PR-AUC: %s (%.4f)" % (chosen["name"], chosen["val_ap_avg"]))

        all_thresholds = {}
        for c in configs:
            p_seeds = []
            for seed, b, pv in zip(c["seeds"], c["boosters"], c["p_val"]):
                thr = choose_thresholds(y_val, pv)
                all_thresholds[f"{c['name']} seed {seed}"] = thr
                p_seeds.append(predict(b, X_test, columns))
                entries.append((f"{c['name']} seed {seed}", p_seeds[-1], thr["alert"]))
            thr = choose_thresholds(y_val, c["p_val_avg"])
            all_thresholds[f"{c['name']} average of {len(c['seeds'])} seeds"] = thr
            entries.append((f"{c['name']} average of {len(c['seeds'])} seeds", np.mean(p_seeds, axis=0), thr["alert"]))

        best_seed, best_booster = saved_c["seeds"][saved_i], saved_c["boosters"][saved_i]
        saved_name = f"{saved_c['name']} seed {best_seed}"
        thresholds = dict(all_thresholds[saved_name], seed=best_seed, config=saved_c["name"])
        out.mkdir(parents=True, exist_ok=True)
        best_booster.save_model(str(out / "scorer.json"))
        (out / "feature_list.json").write_text(json.dumps(columns, indent=2))
        (out / "thresholds.json").write_text(json.dumps(thresholds, indent=2))
        report.update({"saved_model": saved_name, "saved_seed": best_seed, "saved_config": saved_c["name"],
                       "thresholds": thresholds, "all_thresholds": all_thresholds})
        gain = best_booster.get_score(importance_type="gain")
        report["gain"] = {c: float(gain.get(c, 0.0)) for c in columns}
        log("saved %s/scorer.json (%s), feature_list.json, thresholds.json" % (out, saved_name))

    if not entries:
        raise SystemExit("nothing to evaluate: drop --no-train or add --compare-models")
    tables = split_tables(entries, y_test, ts_test, amt_test)
    report["validation_pr_auc"] = validation_rows
    report["test"] = {part: {name: {k: (None if isinstance(v, float) and np.isnan(v) else v)
                                    for k, v in row.items()} for name, row in df.T.to_dict().items()}
                      for part, df in tables.items()}
    report["minutes"] = round((time.time() - t_start) / 60, 1)

    titles = {"overall": "Test, all rows",
              "before": "Test, before %s" % SPLIT_DATE[:10],
              "after": "Test, from %s on (normal traffic nearly stops)" % SPLIT_DATE[:10]}
    md = ["# XGBoost training report", "",
          "Features: `%s`, %d clues. Test: %s rows, %s laundering." % (
              features_path, len(columns), f"{len(y_test):,}", f"{int(y_test.sum()):,}"), "",
          "Precision, recall, F1 and the confusion counts are at each row's alert threshold, chosen on "
          "validation. PR-AUC is average precision. p@K is the share of laundering among the K highest scores.", "",
          "## Validation PR-AUC", "", "| model | validation PR-AUC |", "|---|---|"]
    md += ["| %s | %.4f |" % (k, v) for k, v in validation_rows.items()]
    if "saved_model" in report:
        md += ["", "Configuration with the better averaged validation PR-AUC: **%s**. Saved for the scorer: "
               "**%s** (best single-model validation PR-AUC)." % (report["chosen_config"], report["saved_model"]), "",
               "## Thresholds (validation) and how many test transfers reach them", "",
               "The ring service keeps transfers at or above min(ring, 0.5) in its window, so these counts are "
               "its load.", "",
               "| model | alert | ring | validation F1 at alert | test >= ring | test >= 0.5 |", "|---|---|---|---|---|---|"]
        p_of = {name: p for name, p, _ in entries}
        for name, thr in report["all_thresholds"].items():
            p = p_of[name]
            md.append("| %s | %.4f | %.4f | %.4f | %s | %s |" % (
                name, thr["alert"], thr["ring"], thr["val_f1_at_alert"], f"{int((p >= thr['ring']).sum()):,}",
                f"{int((p >= 0.5).sum()):,}"))
    for part in ("overall", "before", "after"):
        md += ["", "## " + titles[part], "", to_markdown(tables[part])]
    if "gain" in report:
        top = sorted(report["gain"].items(), key=lambda kv: -kv[1])
        md += ["", "## Clue importance (gain) of the saved model", "", "| clue | gain | rank |", "|---|---|---|"]
        md += ["| %s | %.1f | %d |" % (c, g, i + 1) for i, (c, g) in enumerate(top)]
    text = "\n".join(md) + "\n"

    out.mkdir(parents=True, exist_ok=True)
    (out / "training_report.md").write_text(text)
    (out / "training_report.json").write_text(json.dumps(report, indent=2, default=float))
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print("\nvalidation PR-AUC")
        for k, v in validation_rows.items():
            print("  %-60s %.4f" % (k, v))
        for part in ("overall", "before", "after"):
            print("\n" + titles[part])
            print(format_table(tables[part]).to_string())
    log("wrote %s/training_report.md and training_report.json (%.1f min)" % (out, report["minutes"]))
    return report


if __name__ == "__main__":
    main()
