"""Train the GNN second opinion on the candidate subgraph and report it next to XGBoost.

Usage (from the repo root, on the VM inside the Jupyter container or on a laptop):
  python -m scripts.train_gnn --data data/processed --models data/models
  python -m scripts.train_gnn --data /home/ibm-user/shared/data --models /home/ibm-user/shared/models

Reads features.csv (the 32 clues, label, split, amount) and clean.csv (time, sender,
receiver), scores every transfer with models/scorer.json, keeps the transfers at or above
the ring service's expand threshold (the candidate subgraph: the same transfers the ring
service holds in its window), trains the GNN with early stopping on held-out PR-AUC, picks
its threshold on the held-out part, and reports test precision, recall, F1 and PR-AUC for
XGBoost, the GNN and their average on the same transfers. Writes models/gnn.npz and
models/gnn_meta.json.

Two choices make training look like serving:
  - By default the GNN learns from the validation split: its first 75 % by time, with the
    last 25 % for early stopping and the threshold. XGBoost was trained on the train split,
    so its scores there are overconfident, and a GNN fitted on them learns to trust the
    score more than it should live. --fit-split train keeps the old behaviour for comparison.
  - Every split is cut into 24-hour blocks and each block is its own graph (an account in
    two blocks is two nodes). Live, the ring service holds 24 hours, so per-account totals
    such as in-count and out-USD must cover 24 hours in training too, not a whole split.

"Success is honest numbers, not winning": the comparison is printed whichever way it goes.

Run it on the LinuxONE VM, inside the compose image (see docs/next-steps.md). On a Mac,
XGBoost and PyTorch bring two OpenMP runtimes and a full-size run deadlocks; do not use a
Mac for this.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from common.features import FEATURE_NAMES
from common.gnn import NODE_FEATURES, build_graph, make_model, save_model

EXPAND_THRESHOLD = 0.5
DAY_MINUTES = 24 * 60
STOP_SHARE = 0.25       # the end of the fit split, by time: early stopping and the threshold


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def load_rows(data_dir):
    """features.csv joined with the time (ts_min, minutes) and accounts from clean.csv, by txn_id."""
    data_dir = Path(data_dir)
    t0 = time.time()
    feat = pd.read_csv(data_dir / "features.csv",
                       usecols=["txn_id", "split", "label", "amount_usd", *FEATURE_NAMES],
                       dtype={name: "float32" for name in FEATURE_NAMES})
    acct = pd.read_csv(data_dir / "clean.csv", usecols=["txn_id", "ts", "from_acct", "to_acct"],
                       dtype={"from_acct": str, "to_acct": str}, na_filter=False)
    if not np.array_equal(feat.txn_id.to_numpy(), acct.txn_id.to_numpy()):
        acct = acct.set_index("txn_id").reindex(feat.txn_id).reset_index()
    feat["from_acct"] = acct.from_acct.to_numpy()
    feat["to_acct"] = acct.to_acct.to_numpy()
    feat["ts_min"] = pd.to_datetime(acct.ts, format="ISO8601").to_numpy().astype("datetime64[m]").astype("int64")
    log("loaded %s rows in %.0f s" % (f"{len(feat):,}", time.time() - t0))
    return feat


def xgb_scores(models_dir, X):
    import xgboost as xgb
    booster = xgb.Booster()
    booster.load_model(str(Path(models_dir) / "scorer.json"))
    from services.scorer.scorer import best_iteration_range
    # the same trees the live scorer uses (up to the best iteration), so the GNN's
    # xgb_score input is the same number it will see in production
    return booster.predict(xgb.DMatrix(X, feature_names=list(FEATURE_NAMES)),
                           iteration_range=best_iteration_range(booster))


def block_graph(rows, scores):
    """One graph of these transfers, cut into 24-hour blocks that share no nodes.

    A node is "<block>|<account>", so per-account totals cover at most 24 hours, like the
    ring service's window.
    """
    block = (rows.ts_min.to_numpy() // DAY_MINUTES).astype(str)
    src = np.char.add(np.char.add(block, "|"), rows.from_acct.to_numpy().astype(str))
    dst = np.char.add(np.char.add(block, "|"), rows.to_acct.to_numpy().astype(str))
    g = build_graph(src, dst, rows[list(FEATURE_NAMES)].to_numpy(), scores, rows.amount_usd.to_numpy())
    g["y"] = rows.label.to_numpy().astype(np.float32)
    g["xgb"] = np.asarray(scores, dtype=np.float32)
    g["txn_id"] = rows.txn_id.to_numpy()
    return g


def make_graphs(rows, scores, expand, fit_split="validation", stop_share=STOP_SHARE):
    """The candidate subgraphs (XGBoost score >= expand) the GNN is fitted, stopped and tested on.

    fit_split="validation": fit on the first (1 - stop_share) of the validation split by time,
    stop on the rest. XGBoost never trained on these transfers, so their scores are honest.
    fit_split="train": fit on the train split, stop on the validation split.
    The test graph is always the test split.
    """
    split = rows.split.to_numpy()
    if fit_split == "validation":
        t = rows.ts_min.to_numpy()
        in_val = split == "validation"
        lo, hi = t[in_val].min(), t[in_val].max()
        cut = lo + (1 - stop_share) * (hi - lo)
        masks = {"fit": in_val & (t < cut), "stop": in_val & (t >= cut)}
    elif fit_split == "train":
        masks = {"fit": split == "train", "stop": split == "validation"}
    else:
        raise ValueError("fit_split must be 'validation' or 'train', got %r" % fit_split)
    masks["test"] = split == "test"

    keep = scores >= expand
    out = {}
    for name, m in masks.items():
        m = m & keep
        g = block_graph(rows[m], scores[m])
        out[name] = g
        log("%s graph: %s transfers, %s nodes, laundering %s (%.1f%%)" % (
            name, f"{len(g['y']):,}", f"{len(g['accounts']):,}", f"{int(g['y'].sum()):,}",
            100 * g["y"].mean() if len(g["y"]) else 0.0))
        if not g["y"].sum():
            raise SystemExit("the %s graph has no laundering transfers: lower --expand or change --stop-share" % name)
    return out


def to_tensors(g):
    import torch
    return (torch.as_tensor(g["x_node"]), torch.as_tensor(g["x_edge"]),
            torch.as_tensor(g["src_idx"]), torch.as_tensor(g["dst_idx"]), torch.as_tensor(g["y"]))


def metrics(y, p, threshold):
    from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score
    pred = (p >= threshold).astype(int)
    return {"precision": precision_score(y, pred, zero_division=0), "recall": recall_score(y, pred),
            "f1": f1_score(y, pred), "pr_auc": average_precision_score(y, p), "flagged": int(pred.sum())}


def best_f1_threshold(y, p):
    from sklearn.metrics import precision_recall_curve
    prec, rec, thr = precision_recall_curve(y, p)
    f1 = 2 * prec[:-1] * rec[:-1] / (prec[:-1] + rec[:-1] + 1e-12)
    return float(thr[int(np.argmax(f1))])


def train(graphs, hidden=32, layers=2, epochs=80, lr=0.01, patience=15, seed=0):
    import torch
    from sklearn.metrics import average_precision_score

    torch.manual_seed(seed)
    tr, va = graphs["fit"], graphs["stop"]
    model = make_model(tr["x_edge"].shape[1], tr["x_node"].shape[1], hidden, layers)
    model.set_normalisation(tr["x_edge"], tr["x_node"])
    xn, xe, s, d, y = to_tensors(tr)
    vxn, vxe, vs, vd, vy = to_tensors(va)
    pos_weight = torch.tensor([(y == 0).sum().item() / max(1.0, (y == 1).sum().item())])
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    best, best_state, since_best = -1.0, None, 0
    t0 = time.time()
    for epoch in range(1, epochs + 1):
        model.train()
        opt.zero_grad()
        loss = loss_fn(model(xn, xe, s, d), y)
        loss.backward()
        opt.step()
        model.eval()
        with torch.no_grad():
            vp = torch.sigmoid(model(vxn, vxe, vs, vd)).numpy()
        ap = average_precision_score(va["y"], vp)
        if ap > best:
            best, since_best = ap, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            since_best += 1
        if epoch % 10 == 0 or epoch == 1:
            log("epoch %3d  loss %.4f  held-out PR-AUC %.4f  (best %.4f)  %.0f s" % (epoch, loss.item(), ap, best, time.time() - t0))
        if since_best >= patience:
            log("early stop at epoch %d" % epoch)
            break
    model.load_state_dict(best_state)
    model.eval()
    return model, best


def predict_graph(model, g):
    import torch
    xn, xe, s, d, _ = to_tensors(g)
    with torch.no_grad():
        return torch.sigmoid(model(xn, xe, s, d)).numpy()


def main(argv=None):
    p = argparse.ArgumentParser(description="Train the GNN second opinion on the candidate subgraph")
    p.add_argument("--data", required=True, help="folder with features.csv and clean.csv")
    p.add_argument("--models", required=True, help="folder with scorer.json and thresholds.json; gnn files are written here")
    p.add_argument("--expand", type=float, default=EXPAND_THRESHOLD, help="candidate cut-off on the XGBoost score (default: %(default)s)")
    p.add_argument("--hidden", type=int, default=32)
    p.add_argument("--layers", type=int, default=2)
    p.add_argument("--epochs", type=int, default=80)
    p.add_argument("--lr", type=float, default=0.01)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--fit-split", choices=("validation", "train"), default="validation",
                   help="split the GNN learns from (default: %(default)s; XGBoost's scores on 'train' are overconfident)")
    p.add_argument("--stop-share", type=float, default=STOP_SHARE,
                   help="with --fit-split validation, the last share of it by time is held out for early "
                        "stopping and the threshold (default: %(default)s)")
    args = p.parse_args(argv)
    if not 0 < args.stop_share < 1:
        p.error("--stop-share must be between 0 and 1")

    rows = load_rows(args.data)
    scores = xgb_scores(args.models, rows[list(FEATURE_NAMES)].to_numpy())
    graphs = make_graphs(rows, scores, args.expand, args.fit_split, args.stop_share)
    del rows

    model, val_ap = train(graphs, args.hidden, args.layers, args.epochs, args.lr, seed=args.seed)
    held = graphs["stop"]
    vp = predict_graph(model, held)
    gnn_threshold = best_f1_threshold(held["y"], vp)

    thresholds = json.loads((Path(args.models) / "thresholds.json").read_text())
    te = graphs["test"]
    tp = predict_graph(model, te)
    both = (tp + te["xgb"]) / 2
    rows_out = {
        "XGBoost @ alert": metrics(te["y"], te["xgb"], thresholds["alert"]),
        "GNN @ its threshold": metrics(te["y"], tp, gnn_threshold),
        "average of both": metrics(te["y"], both, best_f1_threshold(held["y"], (vp + held["xgb"]) / 2)),
    }
    table = pd.DataFrame(rows_out).T
    print("\ntest candidates: %s transfers (score >= %.2f), %s laundering" % (
        f"{len(te['y']):,}", args.expand, f"{int(te['y'].sum()):,}"))
    print(table.round(4).to_string())

    meta = {"expand_threshold": args.expand, "gnn_threshold": gnn_threshold, "validation_pr_auc": float(val_ap),
            "test": {k: {m: float(v) for m, v in r.items()} for k, r in rows_out.items()},
            "edge_features": [*FEATURE_NAMES, "xgb_score", "log_amount"], "node_features": list(NODE_FEATURES),
            "trained_on": "candidate subgraph of the %s split, 24-hour blocks" % args.fit_split,
            "fit_split": args.fit_split, "seed": args.seed}
    save_model(model, meta, args.models)
    log("saved %s/gnn.npz and gnn_meta.json" % args.models)
    return meta


if __name__ == "__main__":
    main()
