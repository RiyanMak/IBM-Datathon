"""Train the GNN second opinion on the candidate subgraph and report it next to XGBoost.

Usage (from the repo root, on the VM inside the Jupyter container or on a laptop):
  python -m scripts.train_gnn --data data/processed --models data/models
  python -m scripts.train_gnn --data /home/ibm-user/shared/data --models /home/ibm-user/shared/models

Reads features.csv (the 32 clues, label, split, amount) and clean.csv (sender, receiver),
scores every transfer with models/scorer.json, keeps the transfers at or above the ring
service's expand threshold (the candidate subgraph: the same transfers the ring service
holds in its window), builds one graph per split, trains the GNN on the train graph with
early stopping on validation PR-AUC, picks its threshold on validation, and reports test
precision, recall, F1 and PR-AUC for XGBoost, the GNN and their average on the same
transfers. Writes models/gnn.npz and models/gnn_meta.json.

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


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def load_rows(data_dir):
    """features.csv joined with the accounts from clean.csv, by txn_id."""
    data_dir = Path(data_dir)
    t0 = time.time()
    feat = pd.read_csv(data_dir / "features.csv",
                       usecols=["txn_id", "split", "label", "amount_usd", *FEATURE_NAMES],
                       dtype={name: "float32" for name in FEATURE_NAMES})
    acct = pd.read_csv(data_dir / "clean.csv", usecols=["txn_id", "from_acct", "to_acct"],
                       dtype={"from_acct": str, "to_acct": str}, na_filter=False)
    if not np.array_equal(feat.txn_id.to_numpy(), acct.txn_id.to_numpy()):
        acct = acct.set_index("txn_id").reindex(feat.txn_id).reset_index()
    feat["from_acct"] = acct.from_acct.to_numpy()
    feat["to_acct"] = acct.to_acct.to_numpy()
    log("loaded %s rows in %.0f s" % (f"{len(feat):,}", time.time() - t0))
    return feat


def xgb_scores(models_dir, X):
    import xgboost as xgb
    booster = xgb.Booster()
    booster.load_model(str(Path(models_dir) / "scorer.json"))
    return booster.predict(xgb.DMatrix(X, feature_names=list(FEATURE_NAMES)))


def graphs_by_split(rows, scores, expand):
    """One candidate subgraph per split: the transfers scored at or above `expand`."""
    out = {}
    keep = scores >= expand
    for split in ("train", "validation", "test"):
        m = keep & (rows.split.to_numpy() == split)
        sub = rows[m]
        g = build_graph(sub.from_acct.to_numpy(), sub.to_acct.to_numpy(), sub[list(FEATURE_NAMES)].to_numpy(),
                        scores[m], sub.amount_usd.to_numpy())
        g["y"] = sub.label.to_numpy().astype(np.float32)
        g["xgb"] = scores[m].astype(np.float32)
        g["txn_id"] = sub.txn_id.to_numpy()
        out[split] = g
        log("%s graph: %s transfers, %s accounts, laundering %s (%.1f%%)" % (
            split, f"{len(g['y']):,}", f"{len(g['accounts']):,}", f"{int(g['y'].sum()):,}", 100 * g["y"].mean()))
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
    tr, va = graphs["train"], graphs["validation"]
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
            log("epoch %3d  loss %.4f  validation PR-AUC %.4f  (best %.4f)  %.0f s" % (epoch, loss.item(), ap, best, time.time() - t0))
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
    args = p.parse_args(argv)

    rows = load_rows(args.data)
    scores = xgb_scores(args.models, rows[list(FEATURE_NAMES)].to_numpy())
    graphs = graphs_by_split(rows, scores, args.expand)
    del rows

    model, val_ap = train(graphs, args.hidden, args.layers, args.epochs, args.lr, seed=args.seed)
    vp = predict_graph(model, graphs["validation"])
    gnn_threshold = best_f1_threshold(graphs["validation"]["y"], vp)

    thresholds = json.loads((Path(args.models) / "thresholds.json").read_text())
    te = graphs["test"]
    tp = predict_graph(model, te)
    both = (tp + te["xgb"]) / 2
    rows_out = {
        "XGBoost @ alert": metrics(te["y"], te["xgb"], thresholds["alert"]),
        "GNN @ its threshold": metrics(te["y"], tp, gnn_threshold),
        "average of both": metrics(te["y"], both, best_f1_threshold(graphs["validation"]["y"], (vp + graphs["validation"]["xgb"]) / 2)),
    }
    table = pd.DataFrame(rows_out).T
    print("\ntest candidates: %s transfers (score >= %.2f), %s laundering" % (
        f"{len(te['y']):,}", args.expand, f"{int(te['y'].sum()):,}"))
    print(table.round(4).to_string())

    meta = {"expand_threshold": args.expand, "gnn_threshold": gnn_threshold, "validation_pr_auc": float(val_ap),
            "test": {k: {m: float(v) for m, v in r.items()} for k, r in rows_out.items()},
            "edge_features": [*FEATURE_NAMES, "xgb_score", "log_amount"], "node_features": list(NODE_FEATURES),
            "trained_on": "candidate subgraph of the train split", "seed": args.seed}
    save_model(model, meta, args.models)
    log("saved %s/gnn.npz and gnn_meta.json" % args.models)
    return meta


if __name__ == "__main__":
    main()
