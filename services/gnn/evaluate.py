"""Does the GNN help? Ring precision before and after it, against the
dataset's Patterns file (the ring ground truth).

    python -m services.gnn.evaluate --rings real_rescored.json --patterns HI-Small_Patterns.txt

A ring counts as correct when its accounts overlap a true laundering attempt
with Jaccard similarity >= 0.5 (the rule from the architecture doc).
"before" = every ring in the file. "after" = only the rings the GNN kept.
"""
import argparse
import json
from collections import defaultdict
from typing import Dict, List, Set

from services.gnn.transactions import canonical_bank


def parse_patterns(path: str) -> List[dict]:
    """IBM Patterns file -> [{type, accounts(set), n_edges}] with team-style account IDs."""
    attempts, current = [], None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BEGIN LAUNDERING ATTEMPT"):
                typ = line.split("-", 1)[1].strip().split(":")[0].strip()
                current = {"type": typ.lower(), "accounts": set(), "n_edges": 0}
            elif line.startswith("END LAUNDERING ATTEMPT"):
                if current and current["accounts"]:
                    attempts.append(current)
                current = None
            elif current is not None and line:
                p = line.split(",")
                if len(p) < 5:
                    continue
                current["accounts"].update([f"{canonical_bank(p[1])}_{p[2]}", f"{canonical_bank(p[3])}_{p[4]}"])
                current["n_edges"] += 1
    return attempts


def jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / max(len(a | b), 1)


def match(rings: List[dict], attempts: List[dict], min_j: float = 0.5):
    """(precision, recall, per-type [found, total], matched attempt indices)."""
    acct_to_attempts: Dict[str, List[int]] = defaultdict(list)
    for i, a in enumerate(attempts):
        for acct in a["accounts"]:
            acct_to_attempts[acct].append(i)
    hit, correct = set(), 0
    for r in rings:
        accts = r.get("accounts") or []
        if isinstance(accts, str):
            accts = json.loads(accts)
        accts = set(map(str, accts))
        cands = {i for acct in accts for i in acct_to_attempts.get(acct, [])}
        best = max((jaccard(accts, attempts[i]["accounts"]), i) for i in cands) if cands else (0.0, -1)
        if best[0] >= min_j:
            correct += 1
            hit.add(best[1])
    precision = correct / max(len(rings), 1)
    recall = len(hit) / max(len(attempts), 1)
    per_type = defaultdict(lambda: [0, 0])
    for i, a in enumerate(attempts):
        per_type[a["type"]][1] += 1
        if i in hit:
            per_type[a["type"]][0] += 1
    return precision, recall, dict(per_type), hit


def report(rings: List[dict], attempts: List[dict], min_j: float = 0.5) -> dict:
    before = rings
    after = [r for r in rings if r.get("gnn_kept")]
    p0, r0, t0, _ = match(before, attempts, min_j)
    p1, r1, t1, _ = match(after, attempts, min_j)
    print(f"{'':24}{'rings':>8}{'precision':>12}{'recall*':>10}")
    print(f"{'before GNN (all rings)':24}{len(before):>8}{p0:>12.3f}{r0:>10.3f}")
    print(f"{'after GNN (kept)':24}{len(after):>8}{p1:>12.3f}{r1:>10.3f}")
    print("* recall is over ALL attempts in the patterns file; when the rings only cover the test "
          "period, read it as a lower bound.\n")
    print(f"{'per typology':22}{'attempts':>9}{'found before':>14}{'found after':>13}")
    for typ in sorted(t0):
        print(f"{typ:22}{t0[typ][1]:>9}{t0[typ][0]:>14}{t1.get(typ, [0, 0])[0]:>13}")
    if p1 > p0:
        print(f"\nGNN raised ring precision from {p0:.1%} to {p1:.1%} while keeping recall at {r1:.1%} (was {r0:.1%}).")
    else:
        print("\nGNN did not raise precision here. Check the threshold, or train longer / on more data.")
    return {"before": {"rings": len(before), "precision": p0, "recall": r0},
            "after": {"rings": len(after), "precision": p1, "recall": r1}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rings", required=True, help="rescored rings JSON (with gnn_kept)")
    ap.add_argument("--patterns", required=True, help="HI-Small_Patterns.txt")
    ap.add_argument("--min-jaccard", type=float, default=0.5)
    args = ap.parse_args(argv)
    with open(args.rings) as f:
        rings = json.load(f)
    report(rings, parse_patterns(args.patterns), args.min_jaccard)


if __name__ == "__main__":
    main()
