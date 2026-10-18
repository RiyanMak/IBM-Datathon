"""Train the GNN on the cleaned transactions.

    python -m services.gnn.train --txns data/processed/clean.csv --out services/gnn/weights.json

What it does
  1. Loads clean.csv and uses its split column (train / validation / test by
     time, never shuffled). The raw Kaggle CSV also works (60/20/20 by time).
  2. Picks "seed" transfers in the training split. With a score column
     (the scorer's xgb_score), seeds are tier-1 flagged transfers, exactly
     what the ring service hands the GNN. Without one, it uses the labelled
     laundering transfers plus 3x as many random normal ones.
  3. For each batch of seeds, pulls the 2-hop neighbourhood inside a 7-day
     window ending at the seed, builds a subgraph, and trains the GNN to
     label every transfer in it, with BCEWithLogitsLoss and a pos_weight
     for the 1-in-1000 imbalance.
  4. Reports edge-level precision / recall / F1 / AUC on the validation
     split and saves weights as JSON (portable to LinuxONE).
"""
import argparse
import json
import time

import numpy as np
import torch

from services.gnn.graphdata import build_subgraph, to_iso
from services.gnn.model import RingGNN, save_weights_json
from services.gnn.transactions import TxnIndex, load_transactions, time_split


def pick_seeds(df, threshold: float, rng: np.random.Generator, max_seeds: int):
    """Row numbers (into df) to build training subgraphs around."""
    if df["score"].notna().any():
        rows = np.flatnonzero(df["score"].to_numpy() >= threshold)
        how = f"score >= {threshold}"
    else:
        pos = np.flatnonzero(df["label"].to_numpy() == 1)
        neg_pool = np.flatnonzero(df["label"].to_numpy() == 0)
        neg = rng.choice(neg_pool, size=min(len(neg_pool), 3 * len(pos)), replace=False)
        rows = np.concatenate([pos, neg])
        how = "labels (no score column yet)"
    rng.shuffle(rows)
    return rows[:max_seeds], how


def time_batches(index: TxnIndex, seed_rows, size, window_hours):
    """Seeds grouped into batches of up to `size` that all fall inside one
    window, so each batch's subgraph spans one window like at inference."""
    ts = index.ts
    seeds = np.sort(np.asarray(seed_rows))           # rows are in time order
    out, start = [], 0
    for i in range(1, len(seeds) + 1):
        if i == len(seeds) or i - start == size or ts[seeds[i]] - ts[seeds[start]] > window_hours * 3600.0:
            out.append(seeds[start:i])
            start = i
    return out


def subgraph_for_seeds(index: TxnIndex, seed_rows, hops, window_hours, max_edges, rng):
    df = index.df
    accounts = set(df["from"].to_numpy()[seed_rows]) | set(df["to"].to_numpy()[seed_rows])
    t_hi = float(df["ts"].to_numpy()[seed_rows].max())
    t_lo = t_hi - window_hours * 3600.0
    rows = index.neighborhood(accounts, hops=hops, t_lo=t_lo, t_hi=t_hi, max_edges=max_edges, rng=rng)
    rows = np.unique(np.concatenate([rows, np.asarray(seed_rows)]))
    transfers = index.rows_to_transfers(rows)
    if any(t.get("label") is None for t in transfers):
        raise ValueError("Training needs the label column on every transfer.")
    return build_subgraph(transfers)


def auc_score(y, p) -> float:
    y = np.asarray(y); p = np.asarray(p)
    n_pos, n_neg = int(y.sum()), int((1 - y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = p.argsort()
    ranks = np.empty(len(p)); ranks[order] = np.arange(1, len(p) + 1)
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def evaluate(model, index, seed_rows, args, rng):
    ys, ps = [], []
    model.eval()
    with torch.no_grad():
        for batch in time_batches(index, seed_rows, args.seeds_per_batch, args.window_hours):
            g = subgraph_for_seeds(index, batch, args.hops, args.window_hours, args.max_edges, rng)
            ps.append(torch.sigmoid(model(g)).numpy()); ys.append(g.labels.numpy())
    if not ys:
        return {}
    y = np.concatenate(ys); p = np.concatenate(ps)
    pred = p >= 0.5
    tp = int((pred & (y == 1)).sum()); fp = int((pred & (y == 0)).sum()); fn = int((~pred & (y == 1)).sum())
    prec = tp / max(tp + fp, 1); rec = tp / max(tp + fn, 1)
    return {"edges": int(len(y)), "positives": int(y.sum()), "precision": round(prec, 4),
            "recall": round(rec, 4), "f1": round(2 * prec * rec / max(prec + rec, 1e-9), 4),
            "auc": round(auc_score(y, p), 4)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--txns", default="data/processed/clean.csv", help="clean.csv (or the raw Kaggle CSV)")
    ap.add_argument("--out", default="services/gnn/weights.json")
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--window-hours", type=float, default=168.0, help="7 days, like the ring detector")
    ap.add_argument("--max-edges", type=int, default=2000, help="cap per training subgraph")
    ap.add_argument("--seeds-per-batch", type=int, default=16)
    ap.add_argument("--max-seeds", type=int, default=20000, help="cap on training seeds (speed)")
    ap.add_argument("--seed-threshold", type=float, default=0.5, help="score cut for seeds")
    ap.add_argument("--hidden", type=int, default=48)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--nrows", type=int, default=None, help="read only the first N rows (quick test)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    t_start = time.time()

    print(f"loading {args.txns} ...")
    df = load_transactions(args.txns, nrows=args.nrows)
    train_df, val_df, test_df = time_split(df)
    test_start = to_iso(test_df["ts"].iloc[0]) if len(test_df) else "n/a"
    print(f"  {len(df):,} transfers, {int((df['label'] == 1).sum()):,} laundering; "
          f"train/val/test = {len(train_df):,}/{len(val_df):,}/{len(test_df):,} by time")
    print(f"  test split starts {test_start}  (use this as --from-ts in patterns_to_rings)")

    # One index over train+val only: the test split stays untouched.
    trainval = df.loc[train_df.index.union(val_df.index)].sort_values("ts", kind="stable")
    index = TxnIndex(trainval)
    n_train = len(train_df)
    train_seeds, how = pick_seeds(index.df.iloc[:n_train], args.seed_threshold, rng, args.max_seeds)
    val_seeds, _ = pick_seeds(index.df.iloc[n_train:], args.seed_threshold, rng, max(200, args.max_seeds // 4))
    val_seeds = val_seeds + n_train
    print(f"  seeds: {len(train_seeds):,} train, {len(val_seeds):,} val, chosen by {how}")

    train_batches = time_batches(index, train_seeds, args.seeds_per_batch, args.window_hours)
    pos = neg = 0
    for i in rng.choice(len(train_batches), size=min(8, len(train_batches)), replace=False):
        batch = train_batches[i]
        g = subgraph_for_seeds(index, batch, args.hops, args.window_hours, args.max_edges, rng)
        pos += int(g.labels.sum()); neg += int(len(g.labels) - g.labels.sum())
    pos_weight = min(max(neg / max(pos, 1), 1.0), 50.0)
    print(f"  class balance in subgraphs: {pos:,} laundering vs {neg:,} normal -> pos_weight {pos_weight:.1f}")

    model = RingGNN(d=args.hidden)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight))

    history = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        order = rng.permutation(len(train_batches))   # shuffle batches, keep each one in one window
        losses = []
        for batch in (train_batches[i] for i in order):
            g = subgraph_for_seeds(index, batch, args.hops, args.window_hours, args.max_edges, rng)
            opt.zero_grad()
            loss = loss_fn(model(g), g.labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            losses.append(loss.item())
        val = evaluate(model, index, val_seeds, args, rng)
        history.append({"epoch": epoch, "train_loss": round(float(np.mean(losses)), 4), **val})
        print(f"epoch {epoch}: loss {np.mean(losses):.4f}  val precision {val.get('precision')} "
              f"recall {val.get('recall')} f1 {val.get('f1')} auc {val.get('auc')}")

    extra = {"trained_on": args.txns, "seeds_chosen_by": how, "pos_weight": pos_weight,
             "test_start": test_start, "hops": args.hops, "window_hours": args.window_hours,
             "history": history, "train_seconds": round(time.time() - t_start, 1)}
    save_weights_json(model, args.out, extra=extra)
    with open(args.out.replace(".json", "") + "_metrics.json", "w") as f:
        json.dump(extra, f, indent=1)
    print(f"saved {args.out} and metrics in {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
