# DevMaxx_IBM Z Datathon 2026 Project

Mule Ring Hunter: catches money-mule rings in real time. The AI recommends and explains, a person decides.

## Dataset

IBM Transactions for Anti Money Laundering (HI-Small). Synthetic data, no real customers.
https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml

## Data cleaning

The cleaning pipeline turns `HI-Small_Trans.csv` into one typed, time-sorted table and a few reports. It never edits the raw file and never auto-fixes data: if something breaks the data contract, the run stops with exit code 1 and explains why in `data_quality.md`.

Outputs (in the `--out` folder, e.g. `data/processed/`, which is git-ignored):

- `clean.parquet`: every transaction with `txn_id`, account IDs, native amounts, `amount_usd`, label, `self_transfer` flag and `split` (train / validation / test by time). Sample runs write `clean_sample.parquet` instead.
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

[pandas](https://pandas.pydata.org), [pyarrow](https://arrow.apache.org/docs/python/), [NumPy](https://numpy.org), [pytest](https://docs.pytest.org), [redis-py](https://github.com/redis/redis-py), [requests](https://requests.readthedocs.io), [ibm-watsonx-ai](https://ibm.github.io/watsonx-ai-python-sdk/).
