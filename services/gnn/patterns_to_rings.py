"""Turn the dataset's Patterns file into candidate rings in the team's `rings`
format, so the GNN can be tested on real laundering before the ring service
exists.

    python -m services.gnn.patterns_to_rings HI-Small_Patterns.txt real_rings.json \
        --txns data/processed/clean.csv --from-ts 2022-09-08T16:12:00 --decoys 200

--txns      clean.csv (or the raw Kaggle CSV). Used to look up each pattern
            transfer's txn_id and amount_usd, and to build decoys.
--from-ts   keep only attempts whose first transfer is at or after this time.
            Use the test split's start (splits.json "test_start"), so the GNN is
            judged on time it never trained on.
--decoys N  also add N innocent "rings" built from random normal accounts in
            the same period. They stand in for the false candidates a real
            ring detector emits, so evaluate.py can measure precision.
"""
import argparse
import json
import sys

import numpy as np

from services.gnn.graphdata import parse_ts, to_iso
from services.gnn.transactions import TxnIndex, canonical_bank, load_transactions


def read_attempts(path):
    """Patterns file -> [{type, rows:[{ts, from, to, amount_paid, ccy_paid, amount_recv, ccy_recv, fmt}]}]."""
    attempts, cur = [], None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BEGIN LAUNDERING ATTEMPT"):
                typ = line.split("-", 1)[1].strip().split(":")[0].strip().lower()
                cur = {"type": typ, "rows": []}
            elif line.startswith("END LAUNDERING ATTEMPT"):
                if cur and cur["rows"]:
                    attempts.append(cur)
                cur = None
            elif cur is not None and line:
                p = line.split(",")
                if len(p) < 11:
                    continue
                cur["rows"].append({
                    "ts": p[0],
                    "from": f"{canonical_bank(p[1])}_{p[2]}",
                    "to": f"{canonical_bank(p[3])}_{p[4]}",
                    "amount_recv": float(p[5]), "ccy_recv": p[6],
                    "amount_paid": float(p[7]), "ccy_paid": p[8], "fmt": p[9],
                })
    return attempts


def make_ring(edges, shape, ring_id, truth):
    accts = sorted({e["from"] for e in edges} | {e["to"] for e in edges})
    tss = sorted(parse_ts(e["ts"]) for e in edges)
    return {"ring_id": ring_id, "version": 1, "shape": shape, "n_accounts": len(accts),
            "accounts": accts, "edges": edges, "xgb_score": None, "gnn_score": None,
            "reasons": [f"{len(edges)} transfers among {len(accts)} accounts ({shape})"],
            "first_seen": to_iso(tss[0]), "last_seen": to_iso(tss[-1]), "_truth": truth}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("patterns")
    ap.add_argument("out")
    ap.add_argument("--txns", default=None)
    ap.add_argument("--from-ts", default=None)
    ap.add_argument("--decoys", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    attempts = read_attempts(args.patterns)
    t_from = parse_ts(args.from_ts) if args.from_ts else None
    if t_from is not None:
        attempts = [a for a in attempts if min(parse_ts(r["ts"]) for r in a["rows"]) >= t_from]

    index, key = None, {}
    if args.txns:
        df = load_transactions(args.txns)
        index = TxnIndex(df)
        # Match a pattern row to its clean.csv row by (time, from, to, amount paid).
        key = dict(zip(zip(df["ts"].to_numpy(), df["from"].to_numpy(), df["to"].to_numpy(),
                           np.round(df["amount_paid"].to_numpy(), 2)),
                       zip(df["txn_id"].to_numpy(), df["amount_usd"].to_numpy())))

    rings, matched, total = [], 0, 0
    for i, a in enumerate(attempts):
        edges = []
        for j, r in enumerate(a["rows"]):
            hit = key.get((parse_ts(r["ts"]), r["from"], r["to"], round(r["amount_paid"], 2)))
            total += 1
            if hit:
                matched += 1
                txn_id, usd = str(hit[0]), float(hit[1])
            else:
                txn_id, usd = f"P-{i:05d}-{j}", r["amount_paid"]   # unmatched: USD assumed
            edges.append({"txn_id": txn_id, "from": r["from"], "to": r["to"], "amount_usd": round(usd, 2),
                          "ts": to_iso(parse_ts(r["ts"])), "score": None,
                          "ccy_paid": r["ccy_paid"], "ccy_recv": r["ccy_recv"], "fmt": r["fmt"]})
        rings.append(make_ring(edges, a["type"], f"P-{i:05d}", True))

    if args.decoys:
        if index is None:
            ap.error("--decoys needs --txns")
        rng = np.random.default_rng(args.seed)
        d = index.df
        normal = d[d["label"] == 0]
        if t_from is not None:
            normal = normal[normal["ts"] >= t_from]
        counts = normal["from"].value_counts()
        pool = counts[(counts >= 3) & (counts <= 12)].index.to_numpy()
        pick = rng.choice(pool, size=min(args.decoys, len(pool)), replace=False)
        for i, acct in enumerate(pick):
            rows = normal[(normal["from"] == acct) | (normal["to"] == acct)].head(12)
            edges = [{"txn_id": str(r["txn_id"]), "from": r["from"], "to": r["to"],
                      "amount_usd": round(float(r["amount_usd"]), 2), "ts": to_iso(r["ts"]), "score": None,
                      "ccy_paid": r["ccy_paid"], "ccy_recv": r["ccy_recv"], "fmt": r["fmt"]}
                     for r in rows.to_dict("records")]
            shape = "fan-out" if (rows["from"] == acct).mean() > 0.5 else "fan-in"
            rings.append(make_ring(edges, shape, f"D-{i:05d}", False))

    with open(args.out, "w") as f:
        json.dump(rings, f, indent=1)
    n_true = sum(r["_truth"] for r in rings)
    print(f"wrote {len(rings)} rings ({n_true} real attempts, {len(rings) - n_true} decoys) to {args.out}; "
          f"{matched}/{total} pattern transfers matched to a txn_id in the transactions file", file=sys.stderr)


if __name__ == "__main__":
    main()
