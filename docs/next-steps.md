# What is left to build

Updated October 17, 2026, after the replayer, case writer and investigator page were merged.

| Step | Status |
|---|---|
| Cleaning (`scripts/clean_data.py`) | Done. Verified on the real file: 5,078,345 rows, 5,177 laundering. |
| 1 Replayer (`services/replayer/`) | Done. Tested against a real Redis with the real clean file. |
| 2+3 Scorer (`services/scorer/`) | Done. Tested with the replayer on a real Redis: keeps up with speed 600 on a laptop, p99 latency 38 ms. |
| 4 Rings + GNN (`services/rings/`) | Grouping done (window, union-find, hubs, Louvain, shapes, stable ids). Tested behind the scorer on a real Redis; the case writer and page read its output. GNN not started. |
| 5 Case writer (`services/casewriter/`) | Done. |
| 6 Investigator page (`services/page/`) | Done. |
| Training (`notebooks/train_xgboost.ipynb`) | Run on the VM: test F1 0.63, PR-AUC 0.64. Retrain once after the clue-class fix (integer-cent sums) is merged. |
| Platform (`Dockerfile`, `docker-compose.yml`) | Dockerfile fixed (real image, `ibm-user`, tolerant optional installs). Compose not yet run on the VM. |

Nothing flows end to end until the scorer and the ring service exist.

## In this order

### 1. Fix the Dockerfile (10 minutes)

Two lines. Without this `docker compose up` fails on the VM, and containerization is the highest-weighted judging category.

- `FROM registry.linuxone.cloud.marist.edu/l1cc/jupyterlab-image-s390x:latest`
- `USER ibm-user` (the image's user, uid 1000; not `jovyan`)

Also worth doing: split `requirements.txt` so that `snapml` and `ibm-watsonx-ai` install on a separate line that does not fail the whole build if there is no s390x wheel. pandas, torch, xgboost, scikit-learn, networkx and graphviz are already in the image.

### 2. Train the model on the VM

Copy `data/processed/clean.csv` and `splits.json` to `~/shared/data/` on the VM, pull the repo inside `~/shared`, shut down the demo kernels, open `notebooks/train_xgboost.ipynb` and Run All. It writes `scorer.json`, `feature_list.json` and `thresholds.json` to `~/shared/models/` and `features.csv` to `~/shared/data/`.

Then read the outputs together: feature importance (leakage check), precision versus recall balance (class weight), validation versus test gap.

### 3. Scorer (steps 2 and 3)

Reads `txns`, writes `scored`. Can be built before the real model exists by using a dummy model, and tested on a laptop with the replayer feeding it and the page reading it.

Must do:
- Read `txns` with `XREADGROUP` in consumer group `scorers`, `block=1000`, `XACK` after each message. Catch the `BUSYGROUP` error on group creation.
- Decode with `common.streams.message_to_txn`, compute clues with `common.features.ClueBuilder.compute`, score with the XGBoost model loaded from `models/scorer.json` using the column order in `models/feature_list.json`.
- Warm up: before reading the stream, load the rows of `clean.csv` before `test_start` through the same `ClueBuilder` so account histories exist. Otherwise every account looks new at the start of the replay.
- Top 3 reasons: `booster.predict(DMatrix(row), pred_contribs=True)`, take the three largest positive contributions, turn each into a phrase (the phrases also feed Granite).
- Thresholds from `models/thresholds.json`: `flagged = score >= alert`.
- Write to `scored` every `txns` field plus `score`, `flagged` (0 or 1), `reasons` (JSON list), `clues` (JSON object), `scored_at` (same format as `sent_at`, see `common.streams.now_sent_at`).
- Keep the `stats:scorer` hash: `HINCRBY stats:scorer scored 1` and `flagged 1`. The page prefers it over counting the last 500 messages.
- Report p50 and p99 of `scored_at - sent_at`. Target: p99 under 50 ms. The clue class alone measured 0.05 ms at p99.

Scaling demo: `docker compose up -d --scale scorer=3`. The consumer group splits the work.

Measured on a laptop: warm-up of the 4.06 million pre-test rows takes 80 s and about 2 GB of memory per scorer (514,000 account memories), so on the 8 GB VM start scorers one at a time and expect a few minutes each. With three scorers each one holds only a third of the history, so use one for accuracy numbers and three for the throughput screenshot.

Telum (after the scorer works, time-box 2 hours): if `grep nnpa /proc/cpuinfo` finds the accelerator, load `scorer.json` into Snap ML with `import_model(..., input_type="xgb_json")`, score the same 10,000 test transfers both ways, compare time and results. If Snap ML will not install, say so in the video and show CPU latency instead.

### 4. Ring service (step 4)

Reads `scored`, writes `rings`. Grouping first, GNN second.

Part A, grouping:
- Keep a window of transfers with `score >= ring threshold` from the last 24 data hours.
- Every 10 data minutes, union-find over the kept transfers (sender and receiver merge).
- Drop hub accounts with more than 300 counterparties.
- Keep groups of 3 to 50 accounts; split larger ones with Louvain (`networkx.community.louvain_communities`).
- Stable ring IDs: a new group that shares at least half its accounts with an existing ring keeps that `ring_id` and bumps `version`.
- Name the shape with the rules in the guide, most specific first: scatter-gather, cycle, gather-scatter, bipartite, stack, fan-out, fan-in, other.
- Ring score = mean score of its transfers.

Part B, the GNN (plain PyTorch, two message-passing layers with `index_add_`, trained on the candidate subgraph only, `BCEWithLogitsLoss` with `pos_weight`). Train on the VM, save as `.pt` or ONNX, never pickle. Adds `gnn_score` to each ring. Report its test precision and recall next to XGBoost's even if it does not win.

`rings` message fields (the case writer and the page parse exactly these): `ring_id`, `version`, `shape`, `n_accounts`, `accounts` (JSON list), `edges` (JSON list of objects with `txn_id`, `from`, `to`, `amount_usd`, `ts`, `score`), `xgb_score`, `gnn_score`, `reasons` (JSON list), `first_seen`, `last_seen`. All values as text.

### 5. Wire the whole stack on the VM

`docker compose up -d --build`, then `docker compose ps` (screenshot), replay, watch a ring appear on the page, click Freeze, `XRANGE decisions - +` (screenshot). Scale the scorer to 3 and screenshot the throughput difference.

### 6. Numbers for the pitch (Pair 1 collects)

Test precision, recall, F1 and PR-AUC for XGBoost and the $10,000 baseline; GNN precision and recall on the same transfers; share of known laundering attempts caught by a ring and how often the shape name matches `HI-Small_Patterns.txt`; minutes from an attempt's first transfer to its ring appearing; share of money still stoppable after the alert; latency p50 and p99; Telum timing if available; Granite fact-check pass rate.

## Facts everyone needs

- Split cutoffs: validation starts `2022-09-06T13:36:00`, test starts `2022-09-08T16:12:00`. Train 3,046,861 rows (2,297 laundering), validation 1,015,602 (1,082), test 1,015,882 (1,798).
- Shared files on the VM: `~/shared/data/` for `clean.csv`, `splits.json`, `fx_rates.json`, `features.csv`; `~/shared/models/` for `scorer.json`, `feature_list.json`, `thresholds.json`, `gnn.pt`. Inside containers these are `/data/data/` and `/data/models/` (`common/paths.py`).
- No parquet anywhere: the VM image has no pyarrow. Read `clean.csv` with `common.clean_transactions.read_clean_csv`, which restores column types.
- Stream names and the Redis host come from `common/bus.py`; the `txns` message format from `common/streams.py`.
- The raw CSV is 476 MB and the clean CSV 917 MB; both go to the VM by scp, not the Jupyter upload button (200 MB limit).
- Cleaning needs about 5 GB of memory. Check `free -h` on the VM before running anything big there.
