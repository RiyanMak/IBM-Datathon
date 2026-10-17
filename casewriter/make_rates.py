"""
Build rates.json (US dollars per 1 unit of each currency) from the dataset,
using the team guide's rule: look at transfers where one side is US Dollar
and take the median ratio.

Usage (from inside the casewriter folder):
  python3 make_rates.py ~/Downloads/HI-Small_Trans.csv rates.json

Then:
  python3 patterns_to_rings.py ~/Downloads/HI-Small_Patterns.txt real_rings.json --rates rates.json --limit 40

Reads the file one line at a time, so it works on a laptop. Takes a minute or two.
Share rates.json with Pair 1 so everyone uses the same numbers.
"""
import csv
import json
import sys
from collections import defaultdict
from statistics import median

USD = "US Dollar"


def main(trans_path, out_path):
    # pair_ratios[(a, b)] = list of "how many b you get for 1 a"
    pair_ratios = defaultdict(list)
    currencies = set()
    rows = 0
    with open(trans_path, newline="") as fh:
        reader = csv.reader(fh)
        next(reader)  # skip the header row
        for r in reader:
            rows += 1
            # Timestamp, From Bank, Account, To Bank, Account, Amount Received,
            # Receiving Currency, Amount Paid, Payment Currency, Payment Format, Is Laundering
            try:
                amt_recv, ccy_recv = float(r[5]), r[6].strip()
                amt_paid, ccy_paid = float(r[7]), r[8].strip()
            except (ValueError, IndexError):
                continue
            currencies.update((ccy_recv, ccy_paid))
            if ccy_recv != ccy_paid and amt_paid > 0 and amt_recv > 0:
                pair_ratios[(ccy_paid, ccy_recv)].append(amt_recv / amt_paid)
            if rows % 1000000 == 0:
                print("  read %d million rows..." % (rows // 1000000))

    def rate_between(a, b):
        """Median number of b per 1 a, using transfers in either direction."""
        samples = list(pair_ratios.get((a, b), []))
        samples += [1 / x for x in pair_ratios.get((b, a), [])]
        return (median(samples), len(samples)) if samples else (None, 0)

    rates, counts = {USD: 1.0}, {USD: 0}
    for c in sorted(currencies - {USD}):
        r, n = rate_between(c, USD)
        if r is not None:
            rates[c], counts[c] = r, n
    # Currencies never exchanged directly with dollars: go through one that was.
    for c in sorted(currencies - set(rates)):
        best = None
        for via in list(rates):
            r, n = rate_between(c, via)
            if r is not None and (best is None or n > best[1]):
                best = (r * rates[via], n, via)
        if best:
            rates[c], counts[c] = best[0], best[1]
            print("  %s has no direct US Dollar transfers; converted via %s" % (c, best[2]))

    with open(out_path, "w") as fh:
        json.dump({c: round(v, 6) for c, v in sorted(rates.items())}, fh, indent=1)
    print("read %d rows; wrote %d currency rates to %s" % (rows, len(rates), out_path))
    print("%-20s %14s %10s" % ("currency", "USD per unit", "samples"))
    for c in sorted(rates):
        print("%-20s %14.6f %10d" % (c, rates[c], counts[c]))
    missing = currencies - set(rates)
    if missing:
        print("WARNING: no rate found for:", ", ".join(sorted(missing)))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
