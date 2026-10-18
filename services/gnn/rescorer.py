"""Step 4, part B: give each candidate ring a GNN score.

Takes a ring in the team's `rings` format, pulls the transfers within two
hops of it over the previous 7 days (from clean.csv, past rows only), runs
the GNN over that neighbourhood, and fills in `gnn_score`. Rings below the
threshold are "weak".

Two ways to use it:

  1. As a library inside the ring service (simplest, no new stream):

        from services.gnn.rescorer import Rescorer
        rescorer = Rescorer()                       # loads weights + clean.csv once
        ring = rescorer.rescore(ring)               # fills ring["gnn_score"]
        if ring["gnn_kept"]:
            r.xadd(bus.RINGS, ring_to_stream_fields(ring))

  2. As its own service between the ring service and the case writer:

        python -m services.gnn.main                 # reads `candidates`, writes `rings`

     The ring service then writes to the `candidates` stream instead of
     `rings`, and this service forwards the kept rings to `rings`.

  Test mode, no Redis:

        python -m services.gnn.main --file services/gnn/fake_rings.json --txns data/processed/clean.csv

Ring fields (all text on the stream, lists as JSON) are the guide's:
ring_id, version, shape, n_accounts, accounts, edges, xgb_score, gnn_score,
reasons, first_seen, last_seen. Edges are dicts {txn_id, from, to,
amount_usd, ts, score} or lists in that order.
"""
import argparse
import copy
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from services.gnn.graphdata import build_subgraph, edge_dst, edge_src, parse_ts
from services.gnn.model import load_weights_json, ring_score_from_probs
from services.gnn.transactions import TxnIndex, load_transactions

CANDIDATES = "candidates"          # ring service -> gnn (service mode only); add to common/bus.py if used
GROUP = "gnn"
PACKAGED_WEIGHTS = Path(__file__).with_name("weights.json")
EDGE_FIELDS = ["txn_id", "from", "to", "amount_usd", "ts", "score"]   # guide's order for list edges
RING_FIELDS = ["ring_id", "version", "shape", "n_accounts", "accounts", "edges",
               "xgb_score", "gnn_score", "reasons", "first_seen", "last_seen"]


def default_weights_path() -> str:
    try:
        from common.paths import MODELS_DIR
        shared = Path(MODELS_DIR) / "gnn_weights.json"
        if shared.exists():
            return str(shared)
    except Exception:
        pass
    return str(PACKAGED_WEIGHTS)


def default_txns_path() -> Optional[str]:
    try:
        from common.paths import DATA_DIR
        shared = Path(DATA_DIR) / "clean.csv"
        if shared.exists():
            return str(shared)
    except Exception:
        pass
    local = Path("data/processed/clean.csv")
    return str(local) if local.exists() else None


def normalise_edges(edges) -> List[dict]:
    """Edges as the ring service may send them -> list of dicts with the guide's keys."""
    if isinstance(edges, str):
        edges = json.loads(edges)
    if isinstance(edges, dict):
        edges = [dict(v, txn_id=v.get("txn_id", k)) for k, v in edges.items()]
    out = []
    for i, e in enumerate(edges):
        if isinstance(e, dict):
            d = dict(e)
        elif isinstance(e, (list, tuple)):
            d = dict(zip(EDGE_FIELDS, e))
        else:
            raise ValueError(f"Unrecognised edge {i}: {e!r}")
        # Tolerate the other names people use.
        if d.get("from") is None:
            d["from"] = d.get("from_acct", d.get("src"))
        if d.get("to") is None:
            d["to"] = d.get("to_acct", d.get("dst"))
        if d.get("amount_usd") is None:
            d["amount_usd"] = d.get("amount", d.get("amount_paid", d.get("amt_paid")))
        if d.get("score") is None and d.get("xgb_score") is not None:
            d["score"] = d["xgb_score"]
        if d.get("ts") is None:
            d["ts"] = d.get("timestamp", d.get("time"))
        d["txn_id"] = str(d.get("txn_id") if d.get("txn_id") is not None else f"ring-edge-{i}")
        d["from"], d["to"] = str(d["from"]), str(d["to"])
        d["amount_usd"] = float(d["amount_usd"] or 0.0)
        if d.get("score") in ("", None):
            d["score"] = None
        else:
            d["score"] = float(d["score"])
        out.append(d)
    return out


def parse_stream_ring(fields: dict) -> dict:
    """Flat text fields from Redis -> Python ring dict (lists decoded)."""
    ring = dict(fields)
    for k in ("accounts", "edges", "reasons"):
        v = ring.get(k)
        if isinstance(v, str):
            ring[k] = json.loads(v) if v else []
    for k in ("xgb_score", "gnn_score"):
        v = ring.get(k)
        ring[k] = None if v in ("", None) else float(v)
    for k in ("version", "n_accounts"):
        if k in ring and ring[k] not in ("", None):
            ring[k] = int(float(ring[k]))
    return ring


def ring_to_stream_fields(ring: dict) -> dict:
    """Python ring dict -> flat text fields, exactly as the case writer expects.
    Only the guide's fields go on the stream; gnn_kept / gnn_meta stay local."""
    out = {}
    for k in RING_FIELDS:
        if k not in ring:
            continue
        v = ring[k]
        if v is None:
            out[k] = ""
        elif isinstance(v, (list, dict)):
            out[k] = json.dumps(v)
        else:
            out[k] = str(v)
    return out


class Rescorer:
    def __init__(self, weights_path: Optional[str] = None, txn_index: Optional[TxnIndex] = None,
                 txns_path: Optional[str] = None, threshold: float = 0.5, hops: int = 2,
                 window_hours: float = 168.0, top_k: int = 5, max_edges: int = 3000, quiet: bool = False):
        weights_path = weights_path or default_weights_path()
        self.model = load_weights_json(weights_path)
        self.weights_path = weights_path
        if txn_index is None and txns_path is None:
            txns_path = default_txns_path()
        if txn_index is None and txns_path:
            t = time.time()
            df = load_transactions(txns_path)
            txn_index = TxnIndex(df)
            if not quiet:
                print(f"gnn: indexed {len(df):,} transfers from {txns_path} in {time.time() - t:.1f}s",
                      file=sys.stderr)
        elif txn_index is None and not quiet:
            print("gnn: no transaction file found; scoring rings on their own transfers only", file=sys.stderr)
        self.index = txn_index
        self.threshold = threshold
        self.hops = hops
        self.window_hours = window_hours
        self.top_k = top_k
        self.max_edges = max_edges
        self.rng = np.random.default_rng(0)

    def neighbourhood(self, ring_edges: List[dict], accounts: List[str]) -> List[dict]:
        """Ring edges plus everything within `hops` of the ring, in the window.
        Only transfers at or before the ring's last transfer are used (past only)."""
        if self.index is None:
            return [dict(e) for e in ring_edges]
        t_hi = max(parse_ts(e["ts"]) for e in ring_edges)
        t_lo = t_hi - self.window_hours * 3600.0
        rows = self.index.neighborhood(accounts, hops=self.hops, t_lo=t_lo, t_hi=t_hi,
                                       max_edges=self.max_edges, rng=self.rng)
        transfers = self.index.rows_to_transfers(rows) if len(rows) else []
        present = {t["txn_id"] for t in transfers}
        for e in ring_edges:
            if e["txn_id"] in present:
                continue
            row = self.index.by_txn.get(e["txn_id"])
            if row is not None:
                # Dropped by the edge cap or the window: use the indexed row,
                # which has the currencies and format the bare edge lacks.
                transfers.extend(self.index.rows_to_transfers([row]))
            else:
                transfers.append(dict(e))
            present.add(e["txn_id"])
        for t in transfers:               # never let labels leak into scoring
            t.pop("label", None)
        return transfers

    def rescore(self, ring: dict) -> dict:
        """Return a copy of the ring with gnn_score filled in, plus gnn_kept and gnn_meta."""
        ring = copy.deepcopy(ring)
        if isinstance(ring.get("accounts"), str):
            ring["accounts"] = json.loads(ring["accounts"]) if ring["accounts"] else []
        if isinstance(ring.get("reasons"), str):
            ring["reasons"] = json.loads(ring["reasons"]) if ring["reasons"] else []
        edges = normalise_edges(ring.get("edges") or [])
        if not edges:
            ring["gnn_score"] = 0.0
            ring["gnn_kept"] = False
            ring.setdefault("reasons", []).append("GNN: ring had no transfers to score")
            return ring
        accounts = ring.get("accounts") or sorted({edge_src(e) for e in edges} | {edge_dst(e) for e in edges})
        transfers = self.neighbourhood(edges, accounts)
        g = build_subgraph(transfers)
        probs = self.model.edge_probs(g)
        by_txn = {tid: float(p) for tid, p in zip(g.txn_ids, probs)}

        import torch
        edge_scores = {e["txn_id"]: round(by_txn.get(e["txn_id"], 0.0), 4) for e in edges}
        score = ring_score_from_probs(torch.tensor(list(edge_scores.values())), k=self.top_k)

        ring["edges"] = edges                       # normalised, same content
        ring["gnn_score"] = round(score, 4)
        ring["gnn_kept"] = bool(score >= self.threshold)
        ring["gnn_meta"] = {"edge_scores": edge_scores, "neighbourhood_transfers": int(g.num_edges),
                            "accounts_seen": int(g.num_nodes), "threshold": self.threshold}
        verdict = "kept" if ring["gnn_kept"] else "dropped as weak"
        ring.setdefault("reasons", []).append(
            f"GNN ring score {score:.2f} after looking at {g.num_edges} transfers around the ring; {verdict}")
        return ring


# ---- modes -------------------------------------------------------------------

def run_file(rs: Rescorer, path: str, out: Optional[str], drop: bool) -> List[dict]:
    with open(path) as f:
        rings = json.load(f)
    if isinstance(rings, dict):
        rings = [rings]
    t = time.time()
    results = [rs.rescore(r) for r in rings]
    per = (time.time() - t) / max(len(rings), 1)
    kept = [r for r in results if r["gnn_kept"]]
    print(f"rescored {len(results)} rings in {per * 1000:.0f} ms each; kept {len(kept)}, "
          f"dropped {len(results) - len(kept)} below {rs.threshold}", file=sys.stderr)
    payload = kept if drop else results
    if out:
        with open(out, "w") as f:
            json.dump(payload, f, indent=1)
        print(f"wrote {out}", file=sys.stderr)
    else:
        for r in payload:
            print(f"{r.get('ring_id')}  {str(r.get('shape')):16} xgb {r.get('xgb_score')}  "
                  f"gnn {r['gnn_score']}  {'kept' if r['gnn_kept'] else 'weak'}")
    return results


def push_fake(path: str, stream: str) -> int:
    from common import bus
    r = bus.connect()
    with open(path) as f:
        rings = json.load(f)
    for ring in rings:
        r.xadd(stream, ring_to_stream_fields(ring))
    print(f"pushed {len(rings)} rings onto '{stream}'", file=sys.stderr)
    return len(rings)


def run_service(rs: Rescorer, in_stream: str, out_stream: str, drop: bool, redis_client=None, max_messages=None):
    """Consume candidate rings, score them, forward to the rings stream."""
    from common import bus
    r = redis_client or bus.connect()
    try:
        r.xgroup_create(in_stream, GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    print(f"gnn listening on '{in_stream}' as {GROUP}/{consumer}; writing kept rings to '{out_stream}'",
          file=sys.stderr)
    handled = 0
    while max_messages is None or handled < max_messages:
        msgs = r.xreadgroup(GROUP, consumer, {in_stream: ">"}, count=10, block=2000)
        for _, entries in msgs or []:
            for msg_id, fields in entries:
                handled += 1
                try:
                    scored = rs.rescore(parse_stream_ring(fields))
                except Exception as e:          # a bad message: retrying won't help, so ack it
                    print(f"skipped bad ring message {msg_id}: {e}", file=sys.stderr)
                    r.hincrby("stats:gnn", "bad", 1)
                    r.xack(in_stream, GROUP, msg_id)
                    continue
                try:
                    if scored["gnn_kept"] or not drop:
                        r.xadd(out_stream, ring_to_stream_fields(scored))
                except Exception as e:          # Redis trouble: leave it pending, don't lose the ring
                    print(f"could not forward ring message {msg_id}, left unacked: {e}", file=sys.stderr)
                    continue
                r.xack(in_stream, GROUP, msg_id)
                r.hincrby("stats:gnn", "scored", 1)
                r.hincrby("stats:gnn", "kept" if scored["gnn_kept"] else "dropped", 1)
                print(f"{scored.get('ring_id')} v{scored.get('version', '?')}: gnn {scored['gnn_score']} "
                      f"{'kept' if scored['gnn_kept'] else 'dropped'}", file=sys.stderr)
    return handled


def main(argv=None):
    from common import bus
    ap = argparse.ArgumentParser(description="Mule Ring Hunter GNN re-scorer (step 4 part B)")
    ap.add_argument("--weights", default=None, help="weights JSON (default: models/gnn_weights.json or the packaged copy)")
    ap.add_argument("--txns", default=None, help="clean.csv for the neighbourhood (default: /data/data/clean.csv)")
    ap.add_argument("--no-txns", action="store_true", help="score rings on their own transfers only")
    ap.add_argument("--threshold", type=float, default=float(os.environ.get("GNN_THRESHOLD", "0.5")))
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--window-hours", type=float, default=168.0)
    ap.add_argument("--keep-weak", action="store_true", help="forward weak rings too (gnn_score still filled)")
    ap.add_argument("--file", help="rescore rings from a JSON file and print or write them")
    ap.add_argument("--out", help="with --file: write rescored rings here")
    ap.add_argument("--push-fake", help="push rings from a JSON file onto the candidates stream")
    ap.add_argument("--in-stream", default=CANDIDATES)
    ap.add_argument("--out-stream", default=bus.RINGS)
    args = ap.parse_args(argv)

    if args.push_fake:
        return push_fake(args.push_fake, args.in_stream)

    rs = Rescorer(weights_path=args.weights, txns_path="" if args.no_txns else args.txns,
                  threshold=args.threshold, hops=args.hops, window_hours=args.window_hours)
    if args.file:
        run_file(rs, args.file, args.out, drop=not args.keep_weak)
    else:
        run_service(rs, args.in_stream, args.out_stream, drop=not args.keep_weak)


if __name__ == "__main__":
    main()
