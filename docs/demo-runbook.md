# Demo runbook: evidence on the LinuxONE VM

Every command runs on the VM in `~/shared/IBM-Datathon`, in this order. Each row says what to expect, which screenshot to save under `screenshots/`, and which judging point it serves. Do the first block before the model-dependent rows; it needs only Redis, the case writer and the page.

Before starting: `free -h` (the scorer warm-up needs about 2 GB per scorer), `ls ~/shared/data ~/shared/models` (`clean.csv`, `splits.json`, `fx_rates.json`, `HI-Small_Patterns.txt`; `scorer.json`, `feature_list.json`, `thresholds.json`, `gnn.npz`, `gnn_meta.json`).

## 1. Platform and containers (no model needed)

| Step | Command | Expect | Screenshot | Judging point |
|---|---|---|---|---|
| Identity | `uname -m; hostname; grep -c nnpa /proc/cpuinfo` | `s390x`, the VM name, the `nnpa` count (0 is fine; say so) | `01-platform.png` | Runs on LinuxONE; Telum present or not |
| Build and start | `docker compose up -d --build` | six services build from one image | | Containerization |
| Containers up | `docker compose ps` | `redis`, `scorer`, `rings`, `casewriter`, `notifier`, `page` all `Up`; `redis` shows **no** published port, `page` only `127.0.0.1:8501` | `02-compose-ps.png` | Containerization; Security |
| Non-root | `docker compose exec scorer id` | `uid=1000(ibm-user)` | `03-nonroot.png` | Security |
| Secrets | `cat .gitignore; ls -la .env*` | `.env` ignored; only `.env.example` in git | | Security |
| Fake rings end to end | `docker compose exec casewriter python -m services.casewriter.main --push-fake services/casewriter/fake_rings.json` then `docker compose logs --tail 20 casewriter notifier` | three `case for R-000x` lines; the notifier prints three cards (console mode) | | Technical design |
| Page | from the laptop `ssh -L 8501:localhost:8501 linux1@<vm-ip>`, open http://localhost:8501 | three rings with summaries and the source label | `04-page-fake.png` | Community value |

## 2. Scorer and the live replay

| Step | Command | Expect | Screenshot | Judging point |
|---|---|---|---|---|
| Warm-up | `docker compose logs -f scorer` | `warm-up ... rows` then `scoring 'txns' -> 'scored'` (a few minutes) | | Technical design |
| Clean streams | `docker compose exec redis redis-cli FLUSHALL` **only before the real replay**, then `docker compose restart scorer rings casewriter notifier` | fake rings gone; scorer warms up again | | |
| First messages | `docker compose --profile replay run --rm replayer python -m services.replayer.main --limit 100` then `docker compose logs --tail 20 scorer` | scored lines appear | `05-first-scored.png` | Technical design |
| Full replay | `docker compose --profile replay up -d replayer` | about 24 minutes at speed 600 | | |
| Latency | the page's top bar, or `docker compose exec redis redis-cli HGETALL stats:scorer` | `p50_ms`, `p99_ms` under 50 ms (measured 21 / 36 ms) | `06-latency.png` | Compute |
| A ring appears | the page, Cases, open the newest ring; Investigate, Evidence, Decide | picture, reasons, summary, then click **Freeze** | `07-ring.png`, `08-decide.png` | Community value; Transparency |
| Decisions audit | `docker compose exec redis redis-cli XRANGE decisions - +` | the decision with `ring_id`, `version`, `decision`, `note`, `decided_at`, `channel`, `decided_by` | `09-decisions.png` | Transparency; Community value |
| Case packet | `docker compose exec notifier python -m services.notifier.main --packet <ring_id>` | the Markdown packet with every transfer's `txn_id` | `10-packet.png` | Novelty; Community value |

## 3. Scale, resilience, memory

Run these after the full replay, or during a second replay started with `--start` a few data-hours before a known ring.

| Step | Command | Expect | Screenshot | Judging point |
|---|---|---|---|---|
| Throughput baseline | `docker compose run --rm scorer python -m scripts.stress_replayer --limit 200000` | median messages per second into Redis | `11-stress.png` | Scalability |
| Scale out | `docker compose up -d --scale scorer=3` (start one at a time if memory is tight), then `docker compose ps` and `docker compose exec redis redis-cli XINFO GROUPS txns` | three consumers in group `scorers` sharing the lag; `ps` shows `scorer-1..3` | `12-scale.png` | Scalability |
| Compare | replay 60 data-minutes at `--speed 0` with 1 and with 3 scorers; time until `XPENDING txns scorers` is 0 | shorter with three | | Scalability |
| Kill and recover | during a replay `docker compose kill scorer` (one replica), wait 10 s, `docker compose up -d scorer`; `docker compose exec redis redis-cli XPENDING txns scorers` | pending goes back to 0; `XREVRANGE scored + - COUNT 3` shows no gap in `txn_id` | `13-recover.png` | Resilience |
| Replayer resume | `Ctrl+C` the replayer (foreground run), then run again with `--resume` | continues at the next `txn_id`, no duplicates | | Resilience |
| Case writer restart | `docker compose restart casewriter` mid-replay; `docker compose exec redis redis-cli XPENDING rings casewriter` | 0 pending after it comes back | | Resilience |
| Notifier restart | `docker compose restart notifier` | no duplicate cards (the `notifier:messages` hash is restored) | | Resilience |
| Stream cap | after a `--speed 0` replay, `docker compose exec redis redis-cli XLEN txns` and `XLEN scored` | about the configured cap, not every message ever sent | `14-xlen.png` | Memory safety |
| Telum | `python -c "import snapml"` inside the image; if it imports and `nnpa` is present, time Snap ML against XGBoost on 10,000 test rows | a two-row timing table, or the honest "nnpa absent, CPU latency shown" line | `15-telum.png` | Compute |

## 4. Evaluation evidence (the pitch numbers)

After the full replay with one scorer and no `GNN_THRESHOLD`:

```bash
mkdir -p data/evaluation
docker compose exec redis redis-cli --json XRANGE rings - + > /tmp/rings_raw.json   # or the Python line below
docker compose run --rm scorer python - <<'EOF' > data/evaluation/rings_dump.jsonl
import json
from common import bus
for _id, f in bus.connect().xrange("rings", "-", "+"):
    print(json.dumps(f))
EOF
docker compose run --rm scorer python -m scripts.evaluate_rings \
    --patterns /data/data/HI-Small_Patterns.txt --rates /data/data/fx_rates.json --splits /data/data/splits.json \
    --labels /data/data/clean.csv --models /data/models --redis --gnn-sweep \
    --out data/evaluation/evaluation.json --report data/evaluation/evaluation.md
```

Commit `data/evaluation/` (the folder is un-ignored on purpose; the dump is small). `evaluation.md` states the denominators and limitations in plain words; quote the 95-attempt denominator, report the attempts that started before the period on their own line, and never describe the file as ten days of uniform activity.

If the replay started later than `test_start`, add `--replay-start <that time>` so only attempts beginning at or after it count.

## 5. Before the video

- [ ] Screenshots 01 to 15 in `screenshots/`, from the real replay, not the fake feed.
- [ ] `evaluation.md` numbers copied into the pitch: attempts caught, shape match, minutes to alert (median and 90th percentile), money still stoppable, latency p50 and p99, GNN sweep row chosen, Granite fact-check pass rate (`HGETALL stats:casewriter`).
- [ ] `git status` clean on the VM; models and evaluation committed.
