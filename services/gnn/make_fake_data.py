"""Make a small fake world so the GNN can be trained and tested with nothing
from the other pairs. Used by the tests.

Writes three files in the same formats the real ones use:
  fake_txns.csv      raw Kaggle column layout, with Is Laundering labels
  fake_patterns.txt  Patterns file layout (ring ground truth)
  fake_rings.json    candidate rings in the team's rings format (edges as
                     {txn_id, from, to, amount_usd, ts, score}); half real
                     laundering rings, half innocent look-alikes

    python -m services.gnn.make_fake_data --out-dir /tmp/fake
"""
import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone

from services.gnn.transactions import canonical_bank

START = datetime(2022, 9, 1, tzinfo=timezone.utc)
DAYS = 10
BANKS = [f"{b:03d}" for b in range(10, 30)]
CURRENCIES = ["US Dollar"] * 8 + ["Euro", "Yuan"]
FORMATS = ["ACH"] * 6 + ["Credit Card", "Cheque", "Cash", "Wire"]


def raw_ts(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d %H:%M")


def iso_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class World:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.rows = []          # raw csv rows (list of lists), with a hidden index
        self.attempts = []      # (type, [transfer dicts])
        self.acct_counter = 0

    def new_account(self) -> str:
        self.acct_counter += 1
        return f"{self.rng.choice(BANKS)}:{self.acct_counter:09X}"   # raw "bank:acct", split later

    def add(self, src, dst, when: datetime, amount: float, label: int, fmt=None, cur=None) -> dict:
        cur = cur or self.rng.choice(CURRENCIES)
        fmt = fmt or self.rng.choice(FORMATS)
        sb, sa = src.split(":"); db, da = dst.split(":")
        recv = round(amount, 2)
        row = [raw_ts(when), sb, sa, db, da, f"{recv:.2f}", cur, f"{amount:.2f}", cur, fmt, str(label)]
        self.rows.append(row)
        return {"when": when, "from": f"{canonical_bank(sb)}_{sa}", "to": f"{canonical_bank(db)}_{da}",
                "amount": round(amount, 2), "ccy": cur, "fmt": fmt, "label": label, "row": row}

    def rand_time(self, lo_day=0, hi_day=DAYS) -> datetime:
        return START + timedelta(minutes=self.rng.uniform(lo_day * 1440, hi_day * 1440))

    def normal_traffic(self, n_people=1500, n_merchants=80, n_txns=40000):
        people = [self.new_account() for _ in range(n_people)]
        merchants = [self.new_account() for _ in range(n_merchants)]
        employers = [self.new_account() for _ in range(15)]
        for p in people:
            emp = self.rng.choice(employers)
            pay = round(self.rng.uniform(900, 4000), 2)
            for day in (1, 8):
                self.add(emp, p, START + timedelta(days=day, hours=self.rng.uniform(6, 10)), pay, 0, fmt="ACH")
        for _ in range(n_txns):
            src = self.rng.choice(people)
            if self.rng.random() < 0.7:
                dst, amount = self.rng.choice(merchants), round(self.rng.lognormvariate(3.5, 1.0), 2)
            else:
                dst, amount = self.rng.choice(people), round(self.rng.lognormvariate(4.5, 1.0), 2)
            if dst != src:
                self.add(src, dst, self.rand_time(), amount, 0)
        self.people = people

    def fan_out(self):
        src, t0, rows = self.rng.choice(self.people), self.rand_time(0, DAYS - 1), []
        for _ in range(self.rng.randint(4, 8)):
            rows.append(self.add(src, self.new_account(), t0 + timedelta(minutes=self.rng.uniform(5, 180)),
                                 round(self.rng.uniform(1500, 9000), 2), 1, fmt="ACH"))
        self.attempts.append(("FAN-OUT", rows))

    def fan_in(self):
        dst, t0, rows = self.new_account(), self.rand_time(0, DAYS - 1), []
        for _ in range(self.rng.randint(4, 8)):
            rows.append(self.add(self.rng.choice(self.people), dst, t0 + timedelta(minutes=self.rng.uniform(5, 300)),
                                 round(self.rng.uniform(800, 6000), 2), 1, fmt="ACH"))
        self.attempts.append(("FAN-IN", rows))

    def scatter_gather(self):
        src, collector, t0, rows = self.rng.choice(self.people), self.new_account(), self.rand_time(0, DAYS - 1), []
        for _ in range(self.rng.randint(3, 6)):
            mule = self.new_account()
            amt = round(self.rng.uniform(2000, 9000), 2)
            t1 = t0 + timedelta(minutes=self.rng.uniform(5, 120))
            rows.append(self.add(src, mule, t1, amt, 1, fmt="ACH"))
            t2 = t1 + timedelta(minutes=self.rng.uniform(20, 360))
            rows.append(self.add(mule, collector, t2, round(amt * self.rng.uniform(0.90, 0.98), 2), 1, fmt="ACH"))
        self.attempts.append(("SCATTER-GATHER", rows))

    def gather_scatter(self):
        hub, t0, rows, total = self.new_account(), self.rand_time(0, DAYS - 1), [], 0.0
        for _ in range(self.rng.randint(3, 5)):
            amt = round(self.rng.uniform(1000, 5000), 2); total += amt
            rows.append(self.add(self.rng.choice(self.people), hub, t0 + timedelta(minutes=self.rng.uniform(5, 240)),
                                 amt, 1, fmt="ACH"))
        t1 = t0 + timedelta(hours=self.rng.uniform(4, 12))
        for _ in range(self.rng.randint(3, 5)):
            rows.append(self.add(hub, self.new_account(), t1 + timedelta(minutes=self.rng.uniform(5, 120)),
                                 round(total * self.rng.uniform(0.15, 0.3), 2), 1, fmt="ACH"))
        self.attempts.append(("GATHER-SCATTER", rows))

    def cycle(self):
        k = self.rng.randint(3, 6)
        accts = [self.rng.choice(self.people)] + [self.new_account() for _ in range(k - 1)]
        t, amt, rows = self.rand_time(0, DAYS - 2), round(self.rng.uniform(3000, 20000), 2), []
        for i in range(k):
            t += timedelta(hours=self.rng.uniform(1, 10))
            amt = round(amt * self.rng.uniform(0.93, 0.99), 2)
            rows.append(self.add(accts[i], accts[(i + 1) % k], t, amt, 1, fmt="ACH"))
        self.attempts.append(("CYCLE", rows))

    def innocent_star(self) -> dict:
        """A shop many people pay. Looks like fan-in, is normal."""
        merchant, rows = self.new_account(), []
        for _ in range(self.rng.randint(4, 8)):
            rows.append(self.add(self.rng.choice(self.people), merchant, self.rand_time(DAYS * 0.6, DAYS),
                                 round(self.rng.lognormvariate(3.8, 0.6), 2), 0))
        return self.make_ring(rows, "fan-in", truth=False)

    def innocent_payroll(self) -> dict:
        """One employer paying several people once. Looks like fan-out."""
        emp, day, rows = self.new_account(), self.rand_time(DAYS * 0.6, DAYS - 0.2), []
        for _ in range(self.rng.randint(4, 8)):
            rows.append(self.add(emp, self.rng.choice(self.people), day + timedelta(minutes=self.rng.uniform(0, 90)),
                                 round(self.rng.uniform(900, 4000), 2), 0, fmt="ACH"))
        return self.make_ring(rows, "fan-out", truth=False)

    def make_ring(self, rows, shape: str, truth: bool) -> dict:
        accts = sorted({r["from"] for r in rows} | {r["to"] for r in rows})
        edges = [{"txn_id": None, "from": r["from"], "to": r["to"], "amount_usd": r["amount"],
                  "ts": iso_ts(r["when"]), "score": round(self.rng.uniform(0.55, 0.85), 3),
                  "_row": id(r["row"])} for r in rows]
        whens = sorted(r["when"] for r in rows)
        return {"ring_id": None, "version": 1, "shape": shape, "n_accounts": len(accts), "accounts": accts,
                "edges": edges, "xgb_score": round(sum(e["score"] for e in edges) / len(edges), 3),
                "gnn_score": None, "reasons": [f"{len(edges)} flagged transfers among {len(accts)} accounts"],
                "first_seen": iso_ts(whens[0]), "last_seen": iso_ts(whens[-1]), "_truth": truth}


def build(out_dir: str, seed: int = 0) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    w = World(seed)
    w.normal_traffic()
    for _ in range(14): w.fan_out()
    for _ in range(14): w.fan_in()
    for _ in range(12): w.scatter_gather()
    for _ in range(10): w.gather_scatter()
    for _ in range(10): w.cycle()

    cutoff = START + timedelta(days=DAYS * 0.6)
    rings = [w.make_ring(rows, typ.lower(), True) for typ, rows in w.attempts
             if min(r["when"] for r in rows) >= cutoff]
    for i in range(len(rings)):
        rings.append(w.innocent_star() if i % 2 == 0 else w.innocent_payroll())
    w.rng.shuffle(rings)

    # Time order defines txn_id (row number), like the cleaning script.
    w.rows.sort(key=lambda r: r[0])
    row_id = {id(r): i for i, r in enumerate(w.rows)}
    for i, ring in enumerate(rings):
        ring["ring_id"] = f"R-{i:06d}"
        for e in ring["edges"]:
            e["txn_id"] = str(row_id[e.pop("_row")])

    header = ["Timestamp", "From Bank", "Account", "To Bank", "Account", "Amount Received",
              "Receiving Currency", "Amount Paid", "Payment Currency", "Payment Format", "Is Laundering"]
    with open(os.path.join(out_dir, "fake_txns.csv"), "w") as f:
        f.write(",".join(header) + "\n")
        for r in w.rows:
            f.write(",".join(r) + "\n")
    with open(os.path.join(out_dir, "fake_patterns.txt"), "w") as f:
        for typ, rows in w.attempts:
            f.write(f"BEGIN LAUNDERING ATTEMPT - {typ}\n")
            for r in rows:
                f.write(",".join(r["row"]) + "\n")
            f.write(f"END LAUNDERING ATTEMPT - {typ}\n\n")
    with open(os.path.join(out_dir, "fake_rings.json"), "w") as f:
        json.dump(rings, f, indent=1)

    n_l = sum(1 for r in w.rows if r[-1] == "1")
    return {"transfers": len(w.rows), "laundering": n_l, "attempts": len(w.attempts), "rings": len(rings),
            "true_rings": sum(r["_truth"] for r in rings)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/fake")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    s = build(args.out_dir, args.seed)
    print(f"wrote {s['transfers']} transfers ({s['laundering']} laundering), {s['attempts']} attempts, "
          f"{s['rings']} candidate rings ({s['true_rings']} true, {s['rings'] - s['true_rings']} innocent) "
          f"to {args.out_dir}/")


if __name__ == "__main__":
    main()
