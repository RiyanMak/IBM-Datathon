# Demo runbook: resilience, scale and evidence on the LinuxONE VM

The exact commands to run on the VM, what each should print, the screenshot to take and the judging point it serves. Run the items in this order: the first two need only the containers; the rest need the model files in `~/shared/models/` and a replay.

Everything runs from the repo root on the VM (`~/shared/IBM-Datathon`). Redis publishes no port, so every `redis-cli` call runs inside its container: `docker compose exec redis redis-cli …`. Screenshots go in `screenshots/` with the file names below. Never screenshot `.env` or a terminal that shows a token.

Before anything big: `free -h`. The VM has 8 GB; one scorer holds about 2 GB after its warm-up. Big files (`clean.csv` 917 MB, the raw CSV 476 MB) go to `~/shared/data/` by `scp`, not the Jupyter upload button (200 MB limit).

## 0. Start the stack

```bash
docker compose up -d --build                      # Redis, scorer, rings, case writer, page, notifier
docker compose logs -f scorer                     # wait for: scoring 'txns' -> 'scored' as consumer ... (a few minutes)
docker compose --profile replay up -d replayer    # only after that line: the replay of the test period
ssh -L 8501:localhost:8501 linux1@<vm-ip>         # on the laptop, then open http://localhost:8501
```

Start the replayer only after the scorer's warm-up line. Started earlier, the stream fills while the scorer is still loading its 4.06 million history rows and the demo shows a backlog instead of live scoring.

## 1. Platform identity

| | |
|---|---|
| Command | `uname -m; hostname; grep -c nnpa /proc/cpuinfo; free -h` |
| Expected | `s390x`, the VM's host name, a count of lines mentioning `nnpa` (0 means no Telum accelerator is visible to this VM), total memory about 8 GB |
| Screenshot | `screenshots/01-identity.png` |
| Judging point | Runs on IBM LinuxONE; Telum present or not, stated honestly |

## 2. Containers up

| | |
|---|---|
| Command | `docker compose --profile replay ps` |
| Expected | `redis`, `scorer`, `rings`, `casewriter`, `page`, `notifier` and `replayer` `Up` (`redis` `healthy`). The PORTS column is empty for every service except `page`, which shows `127.0.0.1:8501->8501/tcp` |
| Screenshot | `screenshots/02-containers.png` |
| Judging point | Containerization; Security (no Redis port, page only on the loopback) |

## 3. First messages flow

| | |
|---|---|
| Command | `docker compose logs --tail 20 scorer` and `docker compose logs --tail 20 rings casewriter` |
| Expected | scorer: `scored N | flagged N | ring candidates N | ... | latency p50 X ms p99 Y ms | Z msg/s` lines. rings and case writer: rings published, `case for R-0001 v1 (template)` |
| Screenshot | `screenshots/03-flow.png`, plus the page with a ring open: `screenshots/03-page.png` |
| Judging point | Technical design: replayer -> scorer -> rings -> case writer -> page over Redis Streams |

To start a few data-hours before a known ring instead of at the test start: `docker compose --profile replay run --rm replayer python -m services.replayer.main --start 2022-09-09T06:00:00`.

## 4. Latency

| | |
|---|---|
| Command | `docker compose exec redis redis-cli HGETALL stats:scorer` and `docker compose exec redis redis-cli XREVRANGE scored + - COUNT 1` |
| Expected | `p50_ms` and `p99_ms` (laptop measurement: p99 38 ms; target under 50 ms), `scored` and `flagged` counts. The newest `scored` message carries `sent_at` and `scored_at`; their difference is the per-transfer latency |
| Screenshot | `screenshots/04-latency.png` |
| Judging point | Compute: real-time scoring |

## 5. Scale out

One scorer gives the accuracy numbers (it sees every transfer, so its account memory is complete); three give the throughput screenshot.

```bash
docker compose logs scorer | grep "msg/s" | tail -3        # one scorer: note msg/s
docker compose up -d --scale scorer=2                       # add one at a time on the 8 GB VM
docker compose logs -f scorer                               # wait for the new replica's warm-up line
docker compose up -d --scale scorer=3
docker compose exec redis redis-cli XINFO GROUPS txns
docker compose exec redis redis-cli XINFO CONSUMERS txns scorers
free -h
```

| | |
|---|---|
| Expected | `XINFO GROUPS txns`: group `scorers` with `consumers 3`; `XINFO CONSUMERS`: three consumers (the container IDs), each with its own `pending` and a small `idle`. The three replicas' `msg/s` lines add up to more than one scorer's. `free -h` shows memory left |
| Screenshot | `screenshots/05-scale-1.png` (one scorer), `screenshots/05-scale-3.png` (three, with `XINFO CONSUMERS`) |
| Judging point | Scalability: the consumer group splits the stream, no code change |

The live replay at speed 600 sends about 700 messages per second, which one scorer keeps up with. For a throughput difference that shows, replay a slice at full speed while the stack runs: `docker compose --profile replay run --rm replayer python -m services.replayer.main --speed 0 --limit 200000 --resume`.

## 6. Kill and recover (one scorer replica)

Run during a replay, with two or three scorers.

```bash
docker compose ps scorer                                    # note one container name, e.g. ibm-datathon-scorer-2
docker kill ibm-datathon-scorer-2
docker compose exec redis redis-cli XPENDING txns scorers   # the killed consumer still holds its pending messages
sleep 10
docker start ibm-datathon-scorer-2                          # same container, same host name = same consumer
docker compose logs -f --tail 5 scorer                      # warm-up again, then "continuity: fed N already-handled messages"
docker compose exec redis redis-cli XPENDING txns scorers
```

| | |
|---|---|
| Expected | Right after the kill, `XPENDING` shows a pending count with the killed consumer's name. The other replicas keep scoring. After the restart and warm-up the replica first re-reads its own pending messages (`id 0`), scores and acknowledges them: the pending count goes back to 0, so no transfer is lost |
| Screenshot | `screenshots/06-kill.png` (pending > 0), `screenshots/06-recover.png` (pending 0) |
| Judging point | Resilience: at-least-once delivery through the consumer group |

`docker compose kill scorer` would stop every replica at once; kill one container by name. Use `docker start` rather than `docker compose up`, which may recreate the container with a new host name (a new consumer name) and leave the old one's pending messages unclaimed.

## 7. Replayer resume

```bash
docker compose exec redis redis-cli XREVRANGE txns + - COUNT 1     # note txn_id
docker compose --profile replay stop replayer
docker compose exec redis redis-cli XREVRANGE txns + - COUNT 1     # the last txn_id sent before the stop
docker compose --profile replay up -d replayer                     # its command already has --resume
docker compose --profile replay logs --tail 5 replayer
docker compose exec redis redis-cli XREVRANGE txns + - COUNT 3
```

| | |
|---|---|
| Expected | The log says `--resume: last message on 'txns' is txn_id N (data time ...); M rows left`. The newest messages after the restart continue at txn_ids above N, with no repeat of N |
| Screenshot | `screenshots/07-resume.png` |
| Judging point | Resilience: restart without duplicates |

## 8. Stream cap

```bash
docker compose exec redis redis-cli XLEN txns
docker compose exec redis redis-cli XLEN scored
docker compose exec redis redis-cli INFO memory | grep used_memory_human
docker compose run --rm scorer python -m scripts.stress_replayer --limit 100000 --maxlen 20000 --keep
docker compose exec redis redis-cli XLEN txns_stress
docker compose exec redis redis-cli DEL txns_stress
```

| | |
|---|---|
| Expected | `txns` holds at most about 1,100,000 messages (the compose cap: the whole test period, 1,015,882 transfers, about 200 MB, so a slow scorer delays rather than loses messages); `scored` about 300,000 at most (the scorer's cap). The stress run prints `100000 messages in S s = X msg/s`, and `XLEN txns_stress` is about 20,000, not 100,000: the cap trims as it writes |
| Screenshot | `screenshots/08-stream-cap.png` |
| Judging point | Memory safety on a small VM; the `msg/s` line is the replayer's throughput number to quote (laptop: about 29,000 msg/s) |

`scripts/stress_replayer.py` writes to its own stream, which nothing reads; it never touches `txns` and deletes its stream at the end unless `--keep` is given.

## 9. Telum check

```bash
grep -c nnpa /proc/cpuinfo
docker compose run --rm --no-deps scorer python -c "import snapml; print('snapml', snapml.__version__)"
```

| | |
|---|---|
| Expected | Either `nnpa` count above 0 and a Snap ML version: then load `scorer.json` with `import_model(..., input_type="xgb_json")`, score the same 10,000 test transfers with XGBoost and Snap ML, and show a two-row table of time and agreement. Or count 0 / `ModuleNotFoundError`: say so, and show the CPU latency from item 4 instead |
| Screenshot | `screenshots/09-telum.png` |
| Judging point | Compute; claim acceleration only if measured |

There is no timing script in the repo yet (next-steps step 3, time-boxed at 2 hours). Until there is, the honest line is "nnpa absent (or Snap ML not installed), CPU latency shown".

## 10. Decisions audit (page, Slack, console)

Click Freeze on one ring on the page, Dismiss on another in Slack, Needs more info on a third in the console adapter (stop the compose notifier first so two notifiers do not share the `cases` group):

```bash
docker compose stop notifier
docker compose run --rm notifier python -m services.notifier.main --console   # then type: more_info R-0003 check payees
docker compose up -d notifier
docker compose exec redis redis-cli XRANGE decisions - +
```

| | |
|---|---|
| Expected | One message per click with `ring_id`, `version`, `decision`, `note`, `decided_at` (UTC with milliseconds). The Slack one also has `decided_by slack:U...:name` and `channel slack`; the console one `decided_by console:...` and `channel console`; the page one has neither (readers treat it as the page). The page shows Frozen / Dismissed badges for all of them |
| Screenshot | `screenshots/10-decisions.png`, plus the Slack card after the click: `screenshots/10-slack-card.png` |
| Judging point | Transparency (append-only audit trail of who decided what, where and when); Community value (the case reaches the investigator where they work) |

Case packet for the video: `docker compose run --rm notifier python -m services.notifier.main --packet R-0001 > ~/shared/case_R-0001.md` (the same file the Slack card attaches). Screenshot: `screenshots/10-packet.png`.

## 11. Security evidence

```bash
cat .gitignore
docker compose --profile replay ps                 # PORTS: nothing for redis
docker compose exec scorer id
docker compose exec notifier id
ls -l .env
```

| | |
|---|---|
| Expected | `.gitignore` lists `.env`, `data/`, `HI-Small_*.csv`, `HI-Small_*.txt`. No published Redis port; the page only on `127.0.0.1:8501` (reached through the SSH tunnel). `id` prints `uid=1000(ibm-user)`, not root. `.env` exists on the VM only (Slack and watsonx tokens; Slack uses Socket Mode, so no inbound port is opened for it) |
| Screenshot | `screenshots/11-security.png` (not the contents of `.env`) |
| Judging point | Security |

## 12. Evaluation evidence

After a full replay with one scorer and the ring service (`HI-Small_Patterns.txt` in `~/shared/data/`). The output files go to `/data`, which is `~/shared` on the VM; anything written elsewhere disappears with the `--rm` container.

```bash
docker compose run --rm scorer python -m scripts.evaluate_rings --patterns /data/data/HI-Small_Patterns.txt \
    --rates /data/data/fx_rates.json --splits /data/data/splits.json --redis \
    --out /data/eval.json --markdown /data/evaluation.md

docker compose run --rm scorer python -m scripts.evaluate_rings --labels /data/data/clean.csv --models /data/models --redis

# what the GNN adds (replay with GNN_THRESHOLD unset)
docker compose run --rm scorer python -m scripts.evaluate_rings --labels /data/data/clean.csv --models /data/models \
    --patterns /data/data/HI-Small_Patterns.txt --splits /data/data/splits.json --redis --gnn-sweep \
    --out /data/eval-gnn.json --markdown /data/evaluation-gnn.md
```

| | |
|---|---|
| Expected | Printed tables: attempts starting in the period, caught, covered, shape match, median minutes to alert, money still stoppable; the labels block (laptop run over the whole test period: 214 rings, 74 % of ring transfers labelled laundering, 55 % of test-period laundering inside some ring); the GNN sweep table. `~/shared/evaluation.md` holds the same numbers as a report |
| Screenshot | `screenshots/12-evaluation.png` |
| Judging point | Pitch numbers: catch rate, time to alert, money still stoppable |

Batch runs for numbers (not the live demo) use `--maxlen 0` on the replayer and the scorer, so no message is trimmed before it is read.

## If it breaks

| Symptom | Cause | Fix |
|---|---|---|
| A scorer exits with code 137 or the VM stalls during warm-up | Out of memory: each scorer holds about 2 GB; three warming up at once does not fit in 8 GB | One scorer at a time: `--scale scorer=2`, wait for its warm-up line, then `=3`. Check `free -h`. Use one scorer for accuracy numbers |
| The page shows a large backlog, latency in seconds | The replayer started before the scorer's warm-up finished | `docker compose --profile replay stop replayer`, wait for `scoring 'txns' -> 'scored'`, start it again (it resumes) |
| `docker compose logs page` shows a Streamlit import error, or Streamlit is not installed | No pyarrow on s390x | Nothing to do: the page command falls back to the Flask page on the same port automatically |
| `redis-cli: command not found` on the VM | Redis has no published port and no client on the host | Run it in the container: `docker compose exec redis redis-cli ...` |
| Pending messages never go back to 0 after a kill | The container was recreated, so its consumer name changed | `docker start` the same container, or claim the old consumer's messages for a running one (`XAUTOCLAIM txns scorers <consumer> 60000 0`) and restart that replica, which re-reads its own pending messages at startup |
| The notifier posts nothing | No Slack tokens, or `NOTIFIER_ADAPTER=console` | `docker compose logs notifier`; set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` and `NOTIFIER_ADAPTER=slack` in `.env`, then `docker compose up -d notifier` |
