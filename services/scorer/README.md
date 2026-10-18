# Steps 2 and 3: Scorer (clues + XGBoost, live)

**Reads:** Redis stream `txns` (from the replayer) and, at startup, `clean.csv` · **Writes:** Redis stream `scored` (to the ring service and the page) and the `stats:scorer` hash

For every transfer that arrives, the scorer computes the 32 behaviour clues with the same `ClueBuilder` the training notebook used, scores it with the XGBoost model, and writes the score, the flagged bit and the top 3 reasons. One transfer takes well under a millisecond of clue work; batches of up to 200 go through XGBoost in one call.

| File | What it does |
|---|---|
| `scorer.py` | Model loading, warm-up, the scoring loop, the command line |
| `reasons.py` | Clue name → phrase. Pair 1 edits the wording here |
| `main.py` | What `docker compose` runs: `python -m services.scorer.main` |
| `test_scorer.py` | Tests on a fake Redis and a tiny real XGBoost model |

## What it needs

- `models/scorer.json`, `models/feature_list.json`, `models/thresholds.json` from the training notebook (`~/shared/models` on the VM, `/data/models` inside the container).
- `data/clean.csv` and `data/splits.json` for the warm-up: every row before the test start goes through the clue builder so each account has its history when the replay begins. On the VM this takes a few minutes at startup and is logged.

The scorer refuses to start if `feature_list.json` does not match the clue list in `common/features.py`: with a different order the scores would be wrong with no error.

## Try it on a laptop

```bash
# tests (needs: .venv/bin/pip install -r requirements-dev.txt, plus xgboost)
.venv/bin/python -m pytest -q services/scorer

# against a local Redis, with the replayer feeding it
REDIS_HOST=localhost .venv/bin/python -m services.replayer.main --clean data/processed/clean.csv --splits data/processed/splits.json --limit 20000 --speed 0
REDIS_HOST=localhost .venv/bin/python -m services.scorer.main --models path/to/models --clean data/processed/clean.csv --splits data/processed/splits.json --once
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `--models DIR` | `$MODELS_PATH` or `/data/models` | Model folder |
| `--clean PATH` | `$CLEAN_PATH` or `/data/data/clean.csv` | Warm-up rows |
| `--splits PATH` | `$SPLITS_PATH` or `/data/data/splits.json` | The warm-up feeds rows before its `test_start` |
| `--warmup-until TS` | test_start | Feed rows before this time instead |
| `--no-warmup` | off | Skip the file warm-up (tests, laptops) |
| `--no-stream-warmup` | off | After a restart, do not refeed already-handled messages |
| `--consumer NAME` | `$SCORER_NAME` or host name | Name in the consumer group `scorers` |
| `--batch N` | 200 | Messages per read |
| `--maxlen N` | 300000 | Approximate cap on `scored` (`0` = none) |
| `--nthread N` | `$NTHREAD` or 1 | XGBoost threads; a batch of 200 needs no more |
| `--once` | off | Score what is on the stream, then exit |
| `--max-messages N` | all | Stop after N |

## The `scored` message

Every `txns` field as received, plus:

```
score       "0.993412"    laundering probability
flagged     "1" / "0"     score >= alert threshold
reasons     JSON list     up to 3 phrases, only for score >= ring threshold, else []
clues       JSON object   the 32 clue values, 4 decimals
scored_at   "1792345678.456"  wall clock, same format as sent_at
```

Latency per transfer = `scored_at - sent_at`. The p50 and p99 over the last 10,000 messages are printed every 10,000 messages and written to `stats:scorer` (`p50_ms`, `p99_ms`), next to the running `scored` and `flagged` counts the page prefers over counting messages itself.

## Restarts and scaling

After a crash or restart the scorer first scores the messages it was given but never acknowledged, and before that it refeeds every message the group already handled into its memory (bounded by what the `txns` stream still holds), so account histories have no gap.

`docker compose up -d --scale scorer=3` runs three consumers in the same group and Redis splits the queue between them. Each scorer then sees only its share of the transfers, so its per-account memory is partial and clues drift from the offline ones. Use one scorer for accuracy numbers and three to show throughput.
