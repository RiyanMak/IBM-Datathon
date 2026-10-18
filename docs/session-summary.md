# Session summary and hand-off (October 17 to 18, 2026)

Read this first in a new session. Each section stands alone; jump to the one for the feature you are working on. Companion docs: `docs/next-steps.md` (pipeline status and VM commands), `docs/model-improvement-plan.md` (ranked model work), `docs/research-model-training.md` (the research report).

## 1. Where everything stands

- Branch `scorer-rings`, pushed, many commits ahead of `main`, merges cleanly. The PR into `main` is still to be opened. A teammate also works on this branch (GNN rework, `services/gnn` merged from main), so `git pull` before anything.
- **The whole stack runs on the VM in Docker Compose**: Redis, replayer, scorer, ring service (with GNN second opinion), case writer, investigator page. All six containers verified in one `docker compose --profile replay ps`. Page served by the Flask fallback through an SSH tunnel on port 8501. Latency on the VM: p50 21 ms, p99 36 ms once the scorer caught up.
- Test suite: 309 passed, 1 skipped on the VM inside the image (one process); locally about 320 across two processes (see gotchas).
- Model files: `models/` in the repo (commit them if not yet: `scorer.json`, `feature_list.json`, `thresholds.json`, `gnn.npz`, `gnn_meta.json`). On the VM they live in `~/shared/models`, mounted as `/data/models`.
- Data on the VM: `~/shared/data/clean.csv` (917 MB), `splits.json`, `fx_rates.json`, `features.csv`. `HI-Small_Patterns.txt` is NOT there yet (download from Kaggle, scp to `~/shared/data/`).

## 2. The numbers (all from the real HI-Small data, nothing synthetic)

| What | Number |
|---|---|
| Data | 5,078,345 transfers, 5,177 laundering (0.10 %), temporal 60/20/20 split; validation starts 2022-09-06T13:36, test 2022-09-08T16:12 |
| XGBoost, test | precision 0.85, recall 0.50, F1 0.63, PR-AUC 0.64, ROC-AUC 0.99; 1,056 flagged, 158 false alarms; baseline ">= $10k" F1 0.009 |
| Thresholds (validation) | alert 0.984, ring 0.946; score 0.5 gives recall 0.86 at precision 0.09 |
| Model file | 250 trees, best iteration 149; the scorer uses trees up to the best iteration (fixed late; see 5) |
| GNN, test candidates (score >= 0.5, 27,331 transfers) | XGBoost F1 0.67 vs GNN F1 0.39 vs average 0.38: GNN is an honest second opinion, XGBoost decides |
| Rings, real test period (laptop run) | 214 rings, median 5 accounts; 74 % of ring transfers labelled laundering; 55 % of all test laundering inside a ring; core transfers 84 % precise, expanded 46 % (base rate 9 %); scatter-gather and gather-scatter rings 90 to 96 % precise, fan-in 48 % |
| Research framing | Our 0.63 equals IBM's published XGBoost+GFP result on this split (63 to 65); every higher score needed GPU hours |

## 3. Architecture in one paragraph

Replayer streams the test period of `clean.csv` onto Redis stream `txns` at 600x. Scorer (consumer group `scorers`) warms up per-account memory from the 4 million pre-test rows (about 3 min on the VM), computes 32 past-only clues with `common/features.py` (same class as training, leakage-tested), scores with XGBoost, adds SHAP reason phrases for candidates, writes `scored` plus the `stats:scorer` hash. Ring service (one instance) keeps transfers with score >= 0.5 in a 24 h data-time window, groups those >= ring threshold with union-find every 10 data minutes, excludes hubs (> 300 counterparties), expands one hop to members' weaker transfers, names the shape, keeps stable ids, rings keep every transfer ever given, attaches `gnn_score` when `gnn.npz` exists, writes `rings`. Case writer publishes a template case at once and, when Granite is configured, a fact-checked Granite upgrade to `cases`: by default (`GRANITE_MODE=on_demand`) only for rings an investigator opens on the page, which asks through the `granite_requests` stream and gets the text in one call's time on three parallel workers; `GRANITE_MODE=all` adds every other ring in the background. Page reads `scored`, `rings`, `cases`, `decisions`; Freeze / Needs more info / Dismiss write `decisions`.

## 4. Per-feature pointers

- Clues: `common/features.py` (+ `tests/test_features.py`). Window sums in integer cents (float residues once produced negative pass-through).
- Scorer: `services/scorer/` (`scorer.py`, `reasons.py` holds the phrase table, README).
- Rings: `services/rings/` (`rings.py`, README with the rules and the `rings` message fields).
- GNN (ours): `common/gnn.py`, `scripts/train_gnn.py`, `tests/test_gnn.py`. Teammate's alternative: `services/gnn/` (Rescorer class, own weights). Team still has to pick one; the ring service hook is `RingService(gnn=...)`.
- Case writer: `services/casewriter/` (teammate's; `granite.py` has watsonx over HTTPS plus opt-in local llama.cpp; `casewriter.py` has the template-first worker).
- Page: `services/page/` (teammate's Streamlit `app.py`; our `flask_app.py` fallback used on the VM; both share `state.py`, `graph.py`, `decisions.py`, `fmt.py`).
- Evaluation: `scripts/evaluate_rings.py` (`--labels` works now; `--patterns` needs the patterns file).
- Platform: `Dockerfile` (base image pinned via constraints), `docker-compose.yml` (replayer behind the `replay` profile, Redis health gating, page auto-fallback), `docs/next-steps.md` section 5c for all VM commands.
- Notebook: `notebooks/train_xgboost.ipynb` (loads Pair 1's `clean.csv`, 21 cells).

## 5. Gotchas discovered the hard way (do not rediscover)

- **pandas on s390x**: the base image has pandas 3.0.6, which works. `ibm-watsonx-ai` pulls pandas down to 2.3.3, a from-source build that segfaults in every datetime operation. The Dockerfile freezes the base image's versions as pip constraints; `ibm-watsonx-ai` is skipped as a result (the case writer uses HTTPS, not the SDK, so nothing is lost). `scripts/probe_datetime.py` tests the forms.
- **Streamlit** needs pyarrow, no s390x build: the page runs the Flask fallback automatically on the VM.
- **`redis:7`** has no s390x image; `redis:7-alpine` does.
- **Stream caps**: the replayer keeps 1.1 M messages (whole test period); batch runs of the scorer and ring service need `--maxlen 0`, otherwise unconsumed messages are trimmed.
- **Early stopping leaves extra trees**: score with `iteration_range` up to `best_iteration` (scorer and GNN trainer do; anything new that loads `scorer.json` must too, or scores differ from the notebook).
- **Mac only**: PyTorch and XGBoost in one process crash or deadlock (two OpenMP runtimes). Run the suite as two processes locally (everything except `tests/test_gnn.py`, `services/gnn` and the one GNN ring test; then those with `KMP_DUPLICATE_LIB_OK=TRUE`). Train the GNN on the VM, not a Mac.
- **Local Granite** (the image's 350M base model via llama.cpp): about 90 s per ring on one core and rarely valid JSON; opt-in with `GRANITE_LOCAL=1`, off for the demo. Real Granite runs on watsonx with credentials in `.env` on the VM (`WATSONX_APIKEY`, `WATSONX_URL`, `GRANITE_MODEL_ID=ibm/granite-4-h-small`, and `WATSONX_SPACE_ID` or `WATSONX_PROJECT_ID`). Done on Oct 18 on the user's own IBM Cloud account (Essentials plan, about 11 cents per million tokens): the project UI could not attach a Runtime on the fresh account (`no_associated_service_instance_error`), so a deployment space was made through the REST API with the Runtime's CRN and the case writer bills to that space; steps in `services/casewriter/README.md` step 6. Fake rings: Granite passed the fact check 3 of 3. In the first live replay the single background worker fell minutes behind (Granite text for version 1 while the page showed version 2) and the page said "Summary coming" for rings pushed out of its 500-message stream window; branch `granite-on-demand` (Oct 18) fixed both: Granite on request from the page, the evidence page reads `case:<ring_id>` outside the window and shows the computed observations while Granite writes.
- **Dockerfile copies** `common/`, `services/`, `scripts/`, `tests/`: any new top-level folder a container needs must be added.
- **Scaling**: three scorers split the queue, but each holds only a third of the account history; one scorer for accuracy, three for the throughput screenshot.

## 6. What is left, in order

1. Commit `models/` (five files; re-scp `gnn*` from the VM if older than the last retrain) and open the PR from `scorer-rings` into `main`.
2. Download `HI-Small_Patterns.txt`, scp to `~/shared/data/`, run the attempt-level evaluation (`docs/next-steps.md` 5b) for minutes-to-alert and money-still-stoppable.
3. Screenshots: `uname -m`, `docker compose --profile replay ps`, the page with a ring open, `--scale scorer=3` then `ps`, `XRANGE decisions - +` after a Freeze.
4. Decide one GNN for the ring service with the teammate who owns `services/gnn`.
5. Optional page polish (sort by money, decision tiles, suggested next step on top); optional model work per `docs/model-improvement-plan.md`.
6. Video and submission: title exactly "DevMaxx_IBM Z Datathon 2026 Project", by 9 AM Chicago Sunday.

## 7. How to work with the user (for the next session)

Propose before acting on anything non-trivial, keep code simple, no Claude attribution anywhere (no co-author lines, no generated-by comments), commit on the branch with plain messages, and give VM steps one at a time with "paste the output" checkpoints. VM: `ssh -i ~/Downloads/IBMCloudKey.pem linux1@148.100.78.153`, repo at `~/shared/IBM-Datathon`, Jupyter on 38888 (token `Your_Token1`), page via `ssh -L 8501:localhost:8501`.
