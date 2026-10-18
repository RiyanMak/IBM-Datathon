# Model improvement plan (hand-off for a fresh session)

Written October 17, 2026, late evening. Read this with `docs/next-steps.md` (pipeline status) and the research report at `/Users/riyanmaknojia/projects/IBM-Datathon-research/reports/HI Small AML model training.md` (outside the repo on purpose).

## Where things stand

- Branch `scorer-rings`, 11 commits ahead of `main`, clean tree, tests green: 291 in the non-PyTorch process plus 5 (ours) and 20 (teammate's `services/gnn`) in the PyTorch process. It merges into the latest `main` with no conflicts (verified on a scratch copy). Push it and open the PR: `git push -u origin scorer-rings`.
- Model: XGBoost on 32 past-only clues, strict temporal 60/20/20 split. Test (1,015,882 rows, 1,798 laundering): precision 0.85 to 0.90, recall 0.49 to 0.50, F1 0.63, PR-AUC 0.63 to 0.64, ROC-AUC 0.99. Thresholds: alert 0.984, ring 0.952. Score 0.5 gives recall 0.86 at precision 0.09. A laptop-trained copy with the fixed clue class is in the git-ignored `data/models/` (`scorer.json`, `feature_list.json`, `thresholds.json`); `data/processed/` holds the real `clean.csv`, `features.csv`, `splits.json`, `fx_rates.json`.
- Rings on the real test period (laptop, real model): 214 rings, median 5 accounts; 74 % of ring transfers are labelled laundering; 55 % of all test-period laundering sits in a ring; core transfers 84 % precise, expanded ones 46 % (base rate at that score 9 %); scatter-gather and gather-scatter rings 90 to 96 % precise, fan-in 48 %.
- Two GNNs exist: the teammate's `services/gnn` (Rescorer class, two-hop seven-day neighbourhood from clean.csv, trained weights committed) and ours `common/gnn.py` + `scripts/train_gnn.py` (window-only subgraph, loaded by the ring service when `gnn.npz` exists). The team must pick one for the ring service; the ring service already has the hook. Our training run on the real data was still running when this was written; its numbers are not recorded anywhere yet.
- Not on the laptop yet: `HI-Small_Patterns.txt` (download from the Kaggle page to `~/Downloads`). The ring-level evaluation against known attempts needs it; the label-based evaluation works without it.
- Mac-only quirk: PyTorch and XGBoost crash in one process unless XGBoost is imported first and `KMP_DUPLICATE_LIB_OK=TRUE` is set. Run the suite as two processes (see `docs/next-steps.md`). The VM is unaffected.

## What the research found that changes the framing

- Our F1 0.63 matches IBM's own published XGBoost with its Graph Feature Preprocessor on the same split (63.2 to 64.8) and is at or above every plain GNN in the benchmark papers. Everything higher needed GPU hours or is an uncaptured preprint. Say this in the pitch.
- 38 % of HI-Small laundering belongs to no pattern; the simulator labels laundering transitively downstream, so forwarded funds are also positive.
- About a third of test positives fall after September 10, when normal traffic nearly stops; that tail flatters every score. Report test results split at September 11.
- Threshold tuning on validation beats resampling and class weights for boosters; we already do this.
- Published results swing by several F1 points between seeds; average seeds.

## The plan, in order

### 1. Make the numbers defensible (about 30 minutes, no score change)

In `notebooks/train_xgboost.ipynb`:
- Train three seeds (`random_state` 0, 1, 2), average the test probabilities, report the single-seed and averaged results.
- Report PR-AUC as average precision (already `average_precision_score`).
- Add a test table split at `ts < 2022-09-11` and `ts >= 2022-09-11`.
- Add precision at the top 100, 500 and 1,000 flags.
- Per-pattern recall comes from `scripts/evaluate_rings.py --patterns ...` once the patterns file exists.

Save the averaged model as the one the scorer loads only if its validation PR-AUC is at least as good; otherwise keep seed 0 (the scorer loads one JSON model).

### 2. New clues from IBM's ablation (1 to 2 hours including retrain, estimated +1 to +4 F1)

All in `common/features.py`, inside `ClueBuilder.compute_row`, computed before the transfer is remembered (past only). Append the names to `FEATURE_NAMES` at the end so the order of the existing 32 is unchanged; the scorer refuses to start if `feature_list.json` disagrees, so retrain and copy the three model files together.

- `back_and_forth_24h`: how many times the receiver paid the sender in the last 24 h (look up `dst` in `snd.inbound` payers). IBM's SHAP example had two-hop temporal cycles as the top feature.
- `snd_payees_6h`, `rcv_payers_6h`: distinct counterparties over 6 hours (IBM's scatter-gather window). Same deques, tighter cutoff.
- `snd_out_per_payee_24h`: outbound count divided by distinct payees in 24 h (degree-to-fan ratio).
- `snd_amt_std_ratio`: running standard deviation of the sender's outgoing amounts divided by its mean (Welford over lifetime counts; add `sum_out_sq` to `_Account`), and `rcv_amt_std_ratio` for inbound.
- `snd_gap_mean_min`: mean minutes between the sender's outgoing transfers (lifetime; needs `last_out` and a running sum of gaps).

Then: add a unit test per clue in `tests/test_features.py` (the planted scatter-gather story already exists there), rerun the notebook, compare the test table before and after, and keep only what helps. Update `services/scorer/reasons.py` with a phrase for each new clue (it asserts every clue has one).

### 3. Taint features, the high-upside bet (2 to 3 hours, plausibly +2 to +5 F1, unproven on this data)

Goal: feed the model's own scores on earlier inbound transfers back in, so money forwarded from a suspicious arrival is suspicious.

Offline (new notebook section or `scripts/train_taint.py`):
- Stage one with cross-fitting: split the training period into two halves by time; train on each half and score the other, so no training row is scored by a model that saw it. Validation and test rows are scored by the stage-one model trained on all of train (the existing `scorer.json`).
- Walk the rows in time order with a small memory per account of the last 24 h of inbound (minute, score): per transfer compute `snd_taint_max`, `snd_taint_mean` (scores of what arrived at the sender in the last 24 h), `snd_taint_share` (score-weighted share of the sender's inflow), and the receiver mirrors `rcv_taint_max`, `rcv_taint_mean`.
- Stage two: XGBoost on the 32 (or new 39) clues plus the five taint clues. Compare on test.

Live (in `services/scorer/scorer.py`): the scorer already computes every score; add a `TaintMemory` keyed by account holding (minute, score) for inbound transfers of the last 24 h, fed after scoring each message, read before scoring the next. Warm-up must also replay stage-one scores for the pre-test rows (score them with the stage-one model during warm-up, which costs one batched predict per chunk).

Leakage rule: a transfer's taint clues may use only scores of transfers that happened before it, and offline those scores must come from a model that never trained on that row.

### 4. Small tuning (30 minutes of compute, +0 to +1)

Depth 8 and learning rate 0.03 with early stopping; min_child_weight 5; three seeds averaged. Keep whichever validation PR-AUC is best; do not look at test until the choice is made.

### Not worth the time

- A GNN as a route to higher F1. Keep it as the ring re-scorer for the demo only.
- Larger IBM datasets; they do not fit the VM.
- Resampling (SMOTE and friends); the research says threshold tuning already captures the gain for boosters.

## Pipeline items that still gate the demo (from `docs/next-steps.md`)

1. Push and merge `scorer-rings`.
2. Retrain on the VM with the fixed clue class, or copy `data/models/*.json` from the laptop to `~/shared/models/` on the VM.
3. `docker compose up -d --build` on the VM, wait for the scorer's warm-up, then `docker compose --profile replay up -d replayer`; screenshots.
4. Decide which GNN the ring service uses.
5. Download the patterns file and run `scripts/evaluate_rings.py` on the VM replay for the attempt-level pitch numbers.
