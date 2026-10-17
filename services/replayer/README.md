# Step 1: Replayer (clean data → live stream)

**Reads:** `clean.csv` and `splits.json` (from the cleaning pipeline) · **Writes:** Redis stream `txns` (to step 2, the scorer)

The replayer pretends the test period is happening live. It reads the cleaned transactions with `ts >= start` (default: the test start in `splits.json`) and sends them to Redis one message per transaction, in time order, sped up. At the default speed of 600, one data minute takes 0.1 real seconds, so the whole 10-day test period takes about 24 minutes.

It never reads the `label` column, so the scorer cannot see the answers.

| File | What it does |
|---|---|
| `replayer.py` | Loading, pacing, sending, the command-line options |
| `main.py` | What `docker compose` runs: `python -m services.replayer.main` |
| `test_replayer.py` | Automatic checks (uses fakeredis, no Redis server needed) |

The message format lives in `common/streams.py`, so the scorer decodes with the same code.

**Every command below runs from the repo root.**

## Try it on a laptop

```bash
# No Redis needed: print the first 5 messages and stop
.venv/bin/python -m services.replayer.main \
    --clean data/processed/v1/clean.csv --splits data/processed/v1/splits.json --dry-run

# Tests (needs: .venv/bin/pip install -r requirements-dev.txt)
.venv/bin/python -m pytest -q services/replayer
```

With a real Redis on `localhost:6379`:

```bash
REDIS_HOST=localhost .venv/bin/python -m services.replayer.main \
    --clean data/processed/v1/clean.csv --splits data/processed/v1/splits.json --limit 20000
```

Press Ctrl+C to stop. It finishes the current batch, prints the last `txn_id` it sent, and you can continue later with `--resume`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--clean PATH` | `$CLEAN_PATH` or `/data/data/clean.csv` | Cleaned transactions (`~/shared/data/clean.csv` on the VM) |
| `--splits PATH` | `$SPLITS_PATH` or `/data/data/splits.json` | Split cut-offs |
| `--start TS` | `test_start` from splits.json | First data time to send, `YYYY-MM-DDTHH:MM:SS` |
| `--allow-before-test` | off | Allow a `--start` before the test period (those rows were used for training, so scores there look too good) |
| `--speed X` | `$SPEED` or 600 | Data seconds per real second. `0` = as fast as possible |
| `--limit N` | all | Send at most N messages |
| `--resume` | off | Skip everything up to the last `txn_id` already on the stream |
| `--dry-run` | off | Print the first few messages as JSON lines; no Redis |
| `--stream NAME` | `txns` | Stream name (tests use other names) |
| `--maxlen N` | 200000 | Approximate cap on the stream length (`MAXLEN ~`), so Redis memory stays bounded. `0` = no cap |

Status lines go to stderr; `--dry-run` messages go to stdout.

## Message format (`txns` stream)

Exactly these fields, all text:

```
txn_id, ts, from_acct, to_acct, amount_usd, amt_paid, ccy_paid, amt_recv, ccy_recv, fmt, sent_at
```

- `ts` is data time, `2022-09-08T16:12:00` (no timezone).
- Amounts are full precision: `float(text)` gives back exactly the value in clean.csv.
- `sent_at` is the wall-clock Unix time the message was sent, e.g. `1792270279.736`. Latency = scored time minus `sent_at`.
- No `label`, `split` or `source_row`.

In the scorer:

```python
from common.streams import message_to_txn
txn = message_to_txn(fields)   # txn_id int, ts datetime, amounts float, sent_at float
```

The replayer does not send any history before `--start`. The scorer loads that history from `clean.csv` itself to warm up its features.
