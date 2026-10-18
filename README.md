# DevMaxx_IBM Z Datathon 2026 Project

Mule Ring Hunter: catches money-mule rings in real time. The AI recommends and explains, a person decides.

## Dataset

IBM Transactions for Anti Money Laundering (HI-Small). Synthetic data, no real customers.
https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml

## Run the stack on the VM

Needs `~/shared/data/clean.csv` and `splits.json` (from the cleaning below) and `~/shared/models/` (from `notebooks/train_xgboost.ipynb`). From the repo root on the VM:

```bash
docker compose up -d --build                      # Redis, scorer, ring service, case writer, page
docker compose logs -f scorer                     # wait for "scoring 'txns' -> 'scored'": the warm-up takes a few minutes
docker compose --profile replay up -d replayer    # then start the replay of the test period
ssh -L 8501:localhost:8501 linux1@<vm-ip>         # from your laptop, then open http://localhost:8501
```

`docker compose --profile replay ps` lists all six containers; `docker compose up -d --scale scorer=3` runs three scorers on one queue. Each service has a README in `services/<name>/`.

## Data cleaning

The cleaning pipeline turns `HI-Small_Trans.csv` into one typed, time-sorted table and a few reports. It never edits the raw file and never auto-fixes data: if something breaks the data contract, the run stops with exit code 1 and explains why in `data_quality.md`.

Outputs (in the `--out` folder, e.g. `data/processed/`, which is git-ignored):

- `clean.csv`: every transaction with `txn_id`, account IDs, native amounts, `amount_usd`, label, `self_transfer` flag and `split` (train / validation / test by time). Sample runs write `clean_sample.csv` instead. Read it back with `common.clean_transactions.read_clean_csv`, which restores the column types (no parquet: the LinuxONE image has no pyarrow).
- `splits.json`: the two time cutoffs, the split rules and per-split counts.
- `fx_rates.json`: exchange rates (USD per unit) learned from the training rows only.
- `data_quality.md`: measured checks, runtime, memory and limitations.
- `validation.md`: written by the independent checker (see below).

Setup (once):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-cleaning.txt
```

Run from the repo root:

```bash
# quick sample (every 50th row): fine for trying things out, results are NOT final
.venv/bin/python -m scripts.clean_data --input HI-Small_Trans.csv --out data/processed/sample --sample-every 50

# full dataset
.venv/bin/python -m scripts.clean_data --input HI-Small_Trans.csv --out data/processed

# independent check of the outputs (optionally against a rounded rates file)
.venv/bin/python -m scripts.validate_clean --out data/processed --input HI-Small_Trans.csv --reference-rates path/to/rates.json

# tests (cleaning + case writer)
.venv/bin/python -m pytest -q
```

Add `--overwrite` to replace existing outputs. `--nrows N` (the first N rows) also works, but the start of the file has no exchange-rate evidence for some currencies, so it fails on purpose.

## GNN ring re-scorer (step 4 part B)

Gives every ring a second score (`gnn_score`) from a small graph neural network run over the ring and the window transfers touching it, and can hold back weak rings before they reach the case writer. Plain PyTorch, weights as `.npz` (portable to s390x). The model is `common/gnn.py`, trained by `scripts/train_gnn.py` on the transfers XGBoost scores at or above 0.5 in the validation split (where XGBoost's scores are honest), cut into 24-hour blocks like the ring service's window. The ring service loads it from `models/gnn.npz` when present; see `services/rings/README.md`.

```bash
# train (prints XGBoost vs GNN on the same test transfers), writes models/gnn.npz + gnn_meta.json
python -m scripts.train_gnn --data ~/shared/data --models ~/shared/models

# after a replay with no GNN_THRESHOLD: ring numbers with no GNN and at each GNN threshold
python -m scripts.evaluate_rings --labels ~/shared/data/clean.csv --models ~/shared/models \
    --patterns ~/shared/data/HI-Small_Patterns.txt --splits ~/shared/data/splits.json --redis --gnn-sweep
```

Then set `GNN_THRESHOLD` in `.env` from the sweep. `services/gnn/` holds an earlier, separate GNN (PR #6) that the ring service does not use: its weights were trained without the XGBoost score on the transfers, which is the strongest input the ring service has.

## Case writer

Turns each ring from the `rings` Redis stream into a short plain-English case on the `cases` stream. Code computes every number into a facts pack; Granite (watsonx.ai) only rewords it, and a fact check rejects any reply with an invented number, account or date, falling back to a template. Lives in `services/casewriter/`, see its README.

```bash
# print cases for the 3 fake rings, no Redis or Granite needed
python -m services.casewriter.main --file services/casewriter/fake_rings.json

# the service (what docker compose runs): reads `rings`, writes `cases`
python -m services.casewriter.main
```

Redis host and stream names for every service come from `common/bus.py` (`REDIS_HOST`, default `redis` inside compose). Watsonx settings come from `.env` (copy `.env.example`). `data/processed/fx_rates.json` from the cleaning run is the team's only exchange-rate table; `patterns_to_rings.py` reads it so test rings and the scorer agree on every dollar amount.

## Libraries

[pandas](https://pandas.pydata.org), [NumPy](https://numpy.org), [PyTorch](https://pytorch.org), [pytest](https://docs.pytest.org), [redis-py](https://github.com/redis/redis-py), [requests](https://requests.readthedocs.io), [ibm-watsonx-ai](https://ibm.github.io/watsonx-ai-python-sdk/).
