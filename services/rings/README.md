# Step 4: Ring service (grouping; the GNN comes later)

**Reads:** Redis stream `scored` (from the scorer) and `models/thresholds.json` · **Writes:** Redis stream `rings` (to the case writer and the page) and the `stats:rings` hash

One suspicious transfer is a hint; a group of accounts passing money between each other is a case. The ring service keeps the transfers at or above the ring threshold from the last 24 data hours, regroups them every 10 data minutes with union-find, names the shape of each group and publishes it as a ring. A ring keeps its id as it grows; each change bumps its version.

The clock is data time, taken from the transfers themselves, so the service behaves the same at any replay speed and in tests.

| File | What it does |
|---|---|
| `rings.py` | Window, union-find, hubs, Louvain split, shape rules, stable ids, the loop |
| `main.py` | What `docker compose` runs: `python -m services.rings.main` |
| `test_rings.py` | Tests, including one that feeds the output to the case writer and the page |

## Rules, as in the team guide

- Keep a transfer when `score >= ring` from `thresholds.json` (0.946 for the current model). Self-transfers are ignored.
- Drop kept transfers older than 24 data hours.
- Every 10 data minutes, union-find over the kept transfers. Accounts with more than 300 counterparties in the window are hubs and are left out, so a bank cannot glue unrelated rings together.
- Keep groups of 3 to 50 accounts. Larger ones are split with Louvain (networkx) and the pieces that fit are kept.
- A group that shares at least half of its accounts with an existing ring keeps that `ring_id` and gets a new version; otherwise it is a new ring. Ids come from `INCR rings:next_id`, so they stay unique across restarts.
- A ring is published only when it is new or its accounts or transfers changed. When the stream goes quiet, whatever arrived since the last regroup is grouped once more so nothing waits forever.

Shape, most specific rule first: stack (sources → middles → sinks, two or more accounts in each layer), bipartite (two groups, arrows only from one to the other), scatter-gather (A pays several middle accounts, two or more of which pay the same B), gather-scatter (several pay one hub, which pays several), cycle (three or more accounts on a directed loop), fan-out (one account pays three or more), fan-in (three or more pay one), other. Stack and bipartite are checked before scatter-gather because a layered ring always contains small scatter-gather patterns.

## The `rings` message

All values are text. Lists are JSON.

```
ring_id      "R-0042"
version      "2"
shape        "scatter-gather"
n_accounts   "8"
accounts     ["10_8000EBD30", ...]                       sorted
edges        [{"txn_id","from","to","amount_usd","ts","score"}, ...]   in time order
xgb_score    "0.9312"                                     mean score of the ring's transfers
gnn_score    ""                                           empty until the GNN exists
reasons      ["money left within a minute of arriving", ...]   the 3 most frequent phrases
first_seen   "2022-09-08T14:00:00"                        data time
last_seen    "2022-09-08T18:44:00"
```

## Try it on a laptop

```bash
.venv/bin/python -m pytest -q services/rings

# behind the scorer on a local Redis
REDIS_HOST=localhost .venv/bin/python -m services.rings.main --models path/to/models --once
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `--models DIR` | `$MODELS_PATH` or `/data/models` | Folder with `thresholds.json` |
| `--ring-threshold X` | from thresholds.json | Override the threshold |
| `--window-hours H` | 24 | Keep transfers this many data hours |
| `--regroup-minutes M` | 10 | Regroup every M data minutes |
| `--hub-limit N` | 300 | Leave out accounts with more counterparties than N |
| `--min-size`, `--max-size` | 3, 50 | Ring sizes |
| `--consumer NAME` | `$RINGS_NAME` or host name | Name in the consumer group `rings` |
| `--once` | off | Group what is on the stream, then exit |
| `--max-messages N` | all | Stop after N messages |

Run one ring service, not several: the window and the ring ids live in its memory.
