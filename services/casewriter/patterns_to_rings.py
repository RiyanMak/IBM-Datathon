"""
Turn REAL laundering attempts from the dataset into ring messages, so the case
writer can be tested on real data before the ring service (step 4) is ready.

It reads HI-Small_Patterns.txt (from the Kaggle dataset). Each block in that file
is one known laundering attempt with its shape name and its transfers.

Usage (from the repo root):
  python -m services.casewriter.patterns_to_rings /data/data/HI-Small_Patterns.txt real_rings.json --rates data/processed/fx_rates.json
  python -m services.casewriter.main --file real_rings.json

Options:
  --limit 20        how many attempts to convert (default 20)
  --rates FILE      exchange rates, US dollars per 1 unit of each currency. Pass the
                    fx_rates.json written by scripts/clean_data.py (learned on the
                    training split only) so test rings and the scorer agree on every
                    dollar amount. A flat {"Euro": 1.17, ...} file is accepted too.
                    Defaults to /data/data/fx_rates.json if that file exists
                    (~/shared/data on the VM, see common/paths.py). Without rates, only attempts paid in US Dollars are used.

Account IDs and dollar amounts follow the cleaning rules in common/data_contract.py
and common/fx.py: "<bank without leading zeros>_<account>" and
amount_usd = Amount Paid * rate[Payment Currency].

Note: these rings have no model scores yet (XGBoost/GNN aren't trained), so the
summaries will skip the score sentences. That's expected.
"""
import argparse
import json
import os
from datetime import datetime

from common.data_contract import make_account_id
from common.paths import DATA_DIR

DEFAULT_PATTERNS = str(DATA_DIR / "HI-Small_Patterns.txt")
DEFAULT_RATES = str(DATA_DIR / "fx_rates.json")


def account_id(bank, account):
    """Team rule: bank number without leading zeros + '_' + account, e.g. 10_8000EBD30."""
    return make_account_id(bank, account.strip())


def load_rates(path):
    """Read exchange rates as {currency: USD per unit}.

    Accepts the cleaning pipeline's fx_rates.json ({"rates": {ccy: {"rate": x, ...}}})
    and a flat {ccy: x} file.
    """
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("rates"), dict):
        return {ccy: float(info["rate"]) if isinstance(info, dict) else float(info)
                for ccy, info in data["rates"].items()}
    return {ccy: float(x) for ccy, x in data.items()}


def read_attempts(path):
    """Yield (shape, [rows]) for every BEGIN ... END block in the patterns file."""
    shape, rows = None, []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("BEGIN LAUNDERING ATTEMPT"):
                header = line.split("-", 1)[1] if "-" in line else "unknown"
                shape = header.split(":")[0].strip().lower()
                rows = []
            elif line.startswith("END LAUNDERING ATTEMPT"):
                if shape is not None and rows:
                    yield shape, rows
                shape, rows = None, []
            elif shape is not None and line:
                rows.append(line.split(","))


def to_ring(index, shape, rows, rates):
    edges = []
    for n, r in enumerate(rows):
        # Timestamp, From Bank, Account, To Bank, Account, Amount Received,
        # Receiving Currency, Amount Paid, Payment Currency, Payment Format, Is Laundering
        ts = datetime.strptime(r[0].strip(), "%Y/%m/%d %H:%M")
        amount, currency = float(r[7]), r[8].strip()
        if currency == "US Dollar":
            usd = amount
        elif currency in rates:
            usd = amount * rates[currency]
        else:
            return None   # can't convert this attempt honestly; skip it
        edges.append({
            "txn_id": "P%d-%d" % (index, n),
            "from": account_id(r[1], r[2]),
            "to": account_id(r[3], r[4]),
            "amount_usd": round(usd, 2),
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "score": None,
        })
    edges = [e for e in edges if e["from"] != e["to"]]   # drop self-transfers
    if len(edges) < 2:
        return None
    accounts = sorted({e["from"] for e in edges} | {e["to"] for e in edges})
    times = sorted(e["ts"] for e in edges)
    return {
        "ring_id": "R-9%04d" % index,
        "version": 1,
        "shape": shape,
        "n_accounts": len(accounts),
        "accounts": accounts,
        "edges": edges,
        "xgb_score": None,
        "gnn_score": None,
        "reasons": [],
        "first_seen": times[0],
        "last_seen": times[-1],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("patterns_file", nargs="?", default=DEFAULT_PATTERNS)
    p.add_argument("out_file", nargs="?", default="real_rings.json")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--rates", default=DEFAULT_RATES if os.path.exists(DEFAULT_RATES) else None,
                   help="fx_rates.json from scripts/clean_data.py (or a flat {currency: rate} file)")
    args = p.parse_args()
    rates = load_rates(args.rates) if args.rates else {}

    rings, seen, skipped = [], 0, 0
    for shape, rows in read_attempts(args.patterns_file):
        seen += 1
        ring = to_ring(seen, shape, rows, rates)
        if ring is None:
            skipped += 1
            continue
        rings.append(ring)
        if len(rings) >= args.limit:
            break
    with open(args.out_file, "w") as fh:
        json.dump(rings, fh, indent=1)
    shapes = sorted({r["shape"] for r in rings})
    print("read %d attempts, wrote %d rings to %s (skipped %d: other currency or too small)"
          % (seen, len(rings), args.out_file, skipped))
    print("shapes included:", ", ".join(shapes))


if __name__ == "__main__":
    main()
