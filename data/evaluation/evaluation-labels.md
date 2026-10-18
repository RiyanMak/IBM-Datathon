# Ring evaluation against the transfer labels

VM replay of the whole test period, one scorer, committed model (ring threshold 0.9457). Run with `scripts/evaluate_rings.py --labels clean.csv --models /data/models --redis` on 2026-10-18.


| Against the transfer labels | Value |
|---|---|
| Rings (latest versions) | 398 |
| Transfers in rings, labelled laundering | 989 of 2544 (39%) |
| Rings that are mostly laundering | 158 (216 with none) |
| Laundering transfers in the period that sit in a ring | 900 of 1798 (50%) |
| Precision of core transfers / expanded transfers | 61% / 9% (1082 expanded) |

| Shape | Rings | Mean precision |
|---|---|---|
| bipartite | 2 | 24% |
| cycle | 3 | 64% |
| fan-in | 224 | 18% |
| fan-out | 30 | 89% |
| gather-scatter | 10 | 93% |
| other | 122 | 47% |
| scatter-gather | 7 | 90% |
