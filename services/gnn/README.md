# Step 4 part B: GNN ring re-scorer

**Owner:** Ayan · **In:** candidate rings from the ring service (step 4 part A) · **Out:** the same rings with `gnn_score` filled in; weak rings dropped

The ring service groups flagged transfers into candidate rings and scores
each one as the mean of its XGBoost scores. This piece gives every ring a
second opinion. It pulls the transfers within two hops of the ring over the
previous 7 days, runs a graph neural network over that neighbourhood, and
scores the ring on what the network *around* it looks like, including the
normal transfers tier 1 never flagged. Rings under the threshold are dropped,
so the case writer and the page only see strong ones.

```
scored  ──►  ring service (part A)  ──►  GNN (this)  ──►  rings  ──►  casewriter, page
             groups + xgb_score          + gnn_score        (kept rings only)
```

Plain PyTorch, no PyTorch Geometric, so nothing exotic has to install on the
s390x VM. The message passing is one call, `index_add_`. Weights are JSON,
so a model trained on a laptop loads on LinuxONE (big-endian) with no pickle
or byte-order worries.

**Every command below runs from the repo root.** On Ayan's Mac use
`/usr/bin/python3` (the default `python3` there is 3.15, which PyTorch does
not support yet). On the VM, plain `python3` inside the Jupyter image.

---

## What's in this folder

| File | What it does | Do you edit it? |
|---|---|---|
| `rescorer.py` | **The piece itself.** `Rescorer` class for the ring service to import, plus the stream service and the `--file` test mode | No |
| `graphdata.py` | Turns a list of transfers into the tensors the model reads (11 edge features, 8 node features) | No |
| `model.py` | The GNN: 2 message-passing layers, one score per transfer, ring score = mean of the top 5 | No |
| `transactions.py` | Loads `clean.csv` (or the raw Kaggle CSV), finds the 2-hop neighbourhood fast | Only `COLUMN_ALIASES` if a column is renamed |
| `train.py` | Trains on `clean.csv`, uses its `split` column, saves `weights.json` | No |
| `evaluate.py` | Ring precision before vs after the GNN, against `HI-Small_Patterns.txt` | No |
| `patterns_to_rings.py` | Real test rings from the Patterns file (with `txn_id` and `amount_usd` from `clean.csv`) plus innocent decoys | No |
| `make_fake_data.py` | Small fake world for the tests | No |
| `test_gnn.py` | 15 checks, run with pytest or on their own | No |
| `main.py` | What `docker compose` would run: `python -m services.gnn.main` | No |
| `weights.json` | **Trained on the team's clean.csv** (results below). Retrain when the scorer adds `score` | Regenerate with `train.py` |

Shared with the other services: `common/bus.py` (Redis host, stream names),
`common/paths.py` (`/data/data`, `/data/models`), root `requirements.txt`
(torch, numpy, pandas, redis are already there).

---

## Step 1: run the checks (2 min)

```
/usr/bin/python3 -m pytest -q services/gnn
```
or one line per check:
```
/usr/bin/python3 -m services.gnn.test_gnn
```
Expect `15 passed`. The last test runs the stream service against a fake
Redis, so even the Redis path is covered without a server.

## Step 2: see it on real rings (5 min, needs `data/processed/clean.csv`)

The cleaning script's output is the input here. If you don't have it:
`/usr/bin/python3 -m scripts.clean_data --input ~/Downloads/HI-Small_Trans.csv --out data/processed`
(about a minute, 4 GB of memory).

```
/usr/bin/python3 -m services.gnn.patterns_to_rings ~/Downloads/HI-Small_Patterns.txt data/gnn/real_rings.json \
    --txns data/processed/clean.csv --from-ts 2022-09-08T16:12:00 --decoys 200
/usr/bin/python3 -m services.gnn.main --file data/gnn/real_rings.json --txns data/processed/clean.csv \
    --keep-weak --out data/gnn/real_rescored.json
/usr/bin/python3 -m services.gnn.evaluate --rings data/gnn/real_rescored.json --patterns ~/Downloads/HI-Small_Patterns.txt
```

`--from-ts` is the test split's start from `data/processed/splits.json`, so
the model is judged only on time it never trained on. The 200 decoys are
rings built from random normal accounts in the same period; they stand in
for a ring detector's false candidates so precision can be measured.

## Step 3: how the ring service plugs it in (Pair 2, 5 lines)

Option A, as a library inside the ring service. No new stream, nothing else
changes:

```python
from services.gnn.rescorer import Rescorer, ring_to_stream_fields

rescorer = Rescorer()                 # once at start-up: loads weights and clean.csv (~15 s)
...
ring = rescorer.rescore(ring)         # fills ring["gnn_score"], adds ring["gnn_kept"]
if ring["gnn_kept"]:
    r.xadd(bus.RINGS, ring_to_stream_fields(ring))
```

`Rescorer()` finds `/data/models/gnn_weights.json` and `/data/data/clean.csv`
on its own (the packaged `weights.json` is the fallback). `rescore()` never
modifies the input ring and never reads labels. The neighbourhood is limited
to transfers at or before the ring's last transfer, so it only uses the past.
`GNN_THRESHOLD` in the environment overrides the 0.5 cut.

Option B, as its own service. The ring service writes to a `candidates`
stream instead of `rings`, and this service forwards the kept rings:

```
python -m services.gnn.main                    # reads candidates, writes rings
python -m services.gnn.main --push-fake services/casewriter/fake_rings.json   # 3 fake rings onto candidates
```
For this, add `CANDIDATES = "candidates"` to `common/bus.py` and a `gnn`
service to `docker-compose.yml` (copy the `casewriter` block, change the
command to `python -m services.gnn.main`). It keeps `stats:gnn`
(`scored`, `kept`, `dropped`) for the page.

Option A is the recommendation: one process, no extra hop, and the case
writer and page see exactly the `rings` stream they already parse.

## Step 4: the contract (post in the team chat)

> **GNN contract (Ayan).** I take rings in the guide's `rings` format and
> return them with `gnn_score` filled in. Everything else is passed through
> unchanged; I add nothing to the stream fields. Edges can be dicts
> `{txn_id, from, to, amount_usd, ts, score}` or lists in that order, as the
> case writer already accepts. The one thing I need: `txn_id` on each edge
> must be the `clean.csv` txn_id, so I can find the ring's own transfers
> inside the neighbourhood. Pair 2: I can run inside your service (one
> import, one call) or as a separate service reading `candidates`. Which do
> you prefer?

## Step 5: retrain when the scorer exists

Once the scorer writes its score into the cleaned file (or a copy with a
`score` column), retrain so the GNN practises on tier-1 flagged transfers,
exactly what it will see live:

```
/usr/bin/python3 -m services.gnn.train --txns data/processed/clean_scored.csv --out services/gnn/weights.json
```

On the VM it is the same command with `python3`; copy the result to
`~/shared/models/gnn_weights.json`. Training on all 5 million rows takes
about two minutes on a laptop; expect longer on the VM. If memory is tight,
add `--max-edges 1000`; if time is tight, `--max-seeds 5000`.

---

## Results on the team's clean.csv (Oct 17)

Trained on `data/processed/clean.csv` from `scripts.clean_data` (5,078,345
transfers, 5,177 laundering), using its `split` column, in 133 seconds on a
laptop: `--epochs 6 --max-seeds 20000 --hidden 48`, seeds from labels (no
scorer yet), 7-day window, 2 hops. The shipped `weights.json` is this model;
`weights_metrics.json` has every epoch.

Edge level, validation split (every transfer inside the sampled neighbourhoods):

| epoch | precision | recall | F1 | AUC |
|---|---|---|---|---|
| 5 (best) | 0.53 | 0.75 | 0.62 | 0.898 |
| 6 (shipped) | 0.46 | 0.75 | 0.57 | 0.899 |

The dataset paper reports 63% F1 for XGBoost with graph features and 68%
for its best GNN on HI-Small. Precision moves between epochs; recall and
AUC are steady.

Ring level, test split only (from 2022-09-08T16:12:00, never seen in
training): 95 true attempts from the Patterns file plus 200 innocent decoys.

| | rings | precision |
|---|---|---|
| before GNN (all candidates) | 295 | 32% |
| after GNN, threshold 0.5 | 99 | 96% |

All 95 true rings were kept (lowest score 0.66), 196 of 200 decoys were
dropped (mean score 0.09). Every one of the 8 typologies was kept in full.
12 ms per ring with clean.csv indexed in memory (13 s to index).

Say the caveat out loud in the video: the decoys are random normal accounts,
not the near-miss clusters a real ring detector produces. The honest
precision number comes from Pair 2's candidate rings.

---

## If a judge asks

**What is a GNN?** A model that scores a transfer using not just its own
numbers but those of the accounts and transfers connected to it. Ours does
two rounds, so each account sees two hops around itself.

**Why not run the GNN on every transfer?** Too slow, and a transfer's
neighbours have not arrived yet when it is scored. The tree model scores
everything in milliseconds; the GNN re-checks only the small neighbourhood
around each candidate ring.

**What does the ring score mean?** The average of the five strongest
transfer scores inside the ring. One lucky transfer cannot carry a ring and
a few boring ones cannot sink it.

**Why plain PyTorch and JSON weights?** PyTorch Geometric has native code
that may not build on IBM Z; `index_add_` is standard PyTorch. IBM Z is
big-endian, so pickled or raw-binary weights from a laptop can load wrong.
JSON is text.

**How do you know it isn't cheating with the labels?** Labels are read only
in `train.py`. The re-scorer deletes the `label` field from every transfer
before building the graph, the neighbourhood stops at the ring's last
transfer (past only), and the split is by time.

**What is the weak spot?** Slow drips over weeks. The neighbourhood window
is 7 days, so laundering spread over a month looks ordinary. Longer windows
and account-level totals are the next step.

## Fallback

If the GNN does not beat tier 1 on Pair 2's rings, or something will not run
on the VM: `--keep-weak` (or `GNN_THRESHOLD=0`). Every ring is forwarded with
its `gnn_score` attached and nothing downstream changes. Say in the video
that the GNN is wired in and scoring but not yet filtering.

## When something goes wrong

| What you see | What it means | Fix |
|---|---|---|
| `ModuleNotFoundError: services` | Not at the repo root | `cd` to the repo root; run `python -m services.gnn....` |
| `No module named torch` on the Mac | Wrong Python | Use `/usr/bin/python3` |
| `Cannot find account columns` | A renamed column in the transaction file | Add the name to `COLUMN_ALIASES` in `transactions.py` |
| `Unrecognised edge` | Pair 2 sends edges in a shape not covered | Ask for one example message; `normalise_edges` is the one place to extend |
| `0/806 pattern transfers matched` | Patterns file and clean.csv disagree on IDs or amounts | Check both came from the same HI-Small files |
| Memory error loading clean.csv | Small VM | `--nrows 2000000` for training; for serving, the ring service can pass its own `TxnIndex` |
