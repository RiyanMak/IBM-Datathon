# Case writer: update and integration plan

Status as of 2026-10-17. The case writer (step 5) now lives in `services/casewriter/` and is wired into the shared Docker Compose stack (Phase A below is done, together with D1 to D3). Its offline tests run from the repo root with `python -m pytest -q` alongside the cleaning tests (145 pass, 1 skipped without `fakeredis`). Phases B and C (behaviour under a live replay, page conveniences) are still open and are the work for the integration sitting.

Done in the integration commit on `data-cleaning-v1`:

- A1: moved to `services/casewriter/` with package imports and `main.py`; `main()` defaults to the Redis service when no flag is given; the standalone Dockerfile and requirements are gone.
- A2: `granite.py` accepts `WATSONX_APIKEY` or `WATSONX_API_KEY`; root `.env.example` carries `GRANITE_MODEL_ID`; the casewriter `.env.example` is gone.
- A3: `common/bus.py` holds the Redis connection and stream names; the case writer uses it (default host `redis`).
- A4: `requests` and `pytest` added to root `requirements.txt`; Libraries section added to the root README.
- D1: `make_rates.py` removed. The cleaning pipeline's `fx_rates.json` (training rows only) is the team's single rate table; `patterns_to_rings.py` reads it directly (flat files still accepted).
- D2: `patterns_to_rings.py` defaults to `/data/HI-Small_Patterns.txt` and `/data/fx_rates.json`.
- D3: `"other"` shape text added; unknown shapes read "an unusual laundering pattern".

Problems 1 to 4, 10 and 11 in section 2 are therefore closed. The rest of this document is kept as written for the remaining phases.

This document is the checklist for that sitting. It covers:

1. What the case writer does today and what is confirmed working
2. Known integration problems, in the order they will bite
3. The update plan, in build order (phases A to E)
4. Contracts with the steps on either side (ring service and investigator page)
5. What we need from preprocessing before we start
6. Integration-day runbook

The team guide (`Mule Ring Hunter: Team Helper Guide`) is the source of truth for stream names and fields. Where this document and the guide disagree, the guide wins and this document should be fixed.

---

## 1. What exists today

```
casewriter/
  casewriter.py          service loop: rings stream -> cases stream; also --file and --push-fake modes
  facts.py               ring message -> facts pack (every number a summary may contain)
  template.py            fill-in-the-blanks summary (the safety net)
  factcheck.py           rejects Granite replies with invented numbers, accounts or dates
  granite.py             watsonx.ai chat call over HTTPS, system prompt, .env loader
  patterns_to_rings.py   builds test rings from HI-Small_Patterns.txt (test input only)
  make_rates.py          builds rates.json (USD per currency unit) from HI-Small_Trans.csv
  fake_rings.json        3 hand-made rings: scatter-gather, fan-out, cycle
  test_casewriter.py     16 offline tests
  Dockerfile             standalone image (to be removed, see A1)
  requirements.txt       redis, requests
  .env.example           WATSONX_APIKEY, WATSONX_PROJECT_ID, WATSONX_URL, GRANITE_MODEL_ID, REDIS_HOST
```

How a ring becomes a case:

```
rings message -> parse_ring -> build_facts -> template_summary   (always)
                                           -> call_granite -> parse_reply -> check_reply
                                                  pass: source=granite   fail/error: source=template
              -> cases message (ring_id, version, summary JSON, source, check_note, written_at, facts)
```

### Confirmed working (offline)

- Reads `rings` messages in the exact field set from the guide's Handoffs table. Edges may be dicts or lists in the order `txn_id, from, to, amount_usd, ts, score`.
- Writes `cases` messages with the guide's fields plus one extra, `facts` (JSON), for the page.
- Redis usage is correct for a stream consumer: consumer group `casewriter`, `XREADGROUP` with `block=5000`, `XACK` after each message, `BUSYGROUP` tolerated on restart, all values sent as text.
- Fact check catches a changed number, an invented account ID, a date outside the ring window, and a missing part. Rounding (10,000 as 10k, 1.8 as 2) is allowed.
- Template never contains a number that is not in the facts pack (tested on every fake ring).
- Granite failure of any kind (timeout, bad JSON, HTTP error) falls back to the template and records why in `check_note`.
- Account ID rule (`bank without leading zeros + "_" + account`) and the amount rule (Amount Paid times the payment currency rate) in the test tools match the guide's cleaning rules.

### Not yet verified

- A real Granite call. Only the request format has been checked. The default model ID `ibm/granite-3-8b-instruct` is deprecated on watsonx and its successor has been reported withdrawn; the ID must be confirmed against our project at hour 0.
- Behaviour under a live replay (ring versions arriving every second). See section 2.
- Anything inside Compose on the VM.

---

## 2. Known integration problems

Ordered by how soon each one bites once the stack is running.

| # | Problem | Where | Effect | Fix in |
|---|---|---|---|---|
| 1 | Compose runs `python -m services.casewriter.main`; code lives in `casewriter/` with flat imports and no `main.py`; root Dockerfile copies only `common/` and `services/` | `docker-compose.yml:42`, `Dockerfile:11` | Container exits at start | A1 |
| 2 | Redis host defaults to `localhost`; compose sets no `REDIS_HOST` for casewriter | `casewriter/casewriter.py:104` | Connection refused inside the network | A3 |
| 3 | Root `.env.example` uses `WATSONX_API_KEY`; code reads `WATSONX_APIKEY` | `.env.example:2`, `casewriter/granite.py:51` | Granite silently never runs; every case is a template with no error | A2 |
| 4 | `requests` imported but not in root `requirements.txt`; neither library linked in root README | `casewriter/requirements.txt` | Build failure or a submission-rule miss | A4 |
| 5 | Ring service republishes a ring every 10 data-minutes while it grows (one message per real second at SPEED=600); each version triggers a multi-second Granite call in a single-threaded loop | `casewriter/casewriter.py:132` | Cases lag several versions behind during the demo | B1, B2, B3 |
| 6 | Granite timeout is 30 s and applied twice (IAM token, then chat). Guide says 15 s | `casewriter/granite.py:23` | A hung network blocks the service up to 60 s per ring | B4 |
| 7 | IAM token fetched on every call | `casewriter/granite.py:75` | Doubles latency, risks IAM rate limits | B4 |
| 8 | Only reads `>` (new messages); pending messages after a crash are never re-read | `casewriter/casewriter.py:132` | Rings lost on container restart | B5 |
| 9 | `hours_to_pass_on` is first-in to first-out (0.7 h on the demo ring); the pitch and flowchart say 1.8 h, which is first-in to last-out | `casewriter/facts.py:152` | Video narration and screen disagree | B7 |
| 10 | Ring service will send shape `Other`; `SHAPE_TEXT` has no entry, so the summary says "a unusual laundering pattern" | `casewriter/template.py:11` | Reads badly on unnamed rings | D3 |
| 11 | `make_rates.py` is the guide's cleaning rule 4 but lives in the casewriter folder; Pair 1 will write their own | `casewriter/make_rates.py` | Test rings and scorer use different dollar amounts | D1 |
| 12 | `written_at` is local wall-clock time; nothing carries data time | `casewriter/casewriter.py:75` | Time-to-alert metric cannot be computed from cases | B6 |

---

## 3. Update plan, in build order

### Phase A: make it start in the shared stack

Do this first on integration day. About 30 minutes.

- **A1. Relocate and package.** Move `casewriter/*.py` and `fake_rings.json` to `services/casewriter/`. Add `services/casewriter/main.py`:
  ```python
  from services.casewriter.casewriter import main
  if __name__ == "__main__":
      main()
  ```
  Change imports to package form (`from services.casewriter.facts import ...`). Make `main()` default to `--redis` when no flag is given, so the compose command needs no arguments. Delete `casewriter/Dockerfile` and the README's Step 8 compose block; the root Dockerfile and compose are the only build path.
- **A2. Environment names.** In `granite.py`, read the key as `WATSONX_APIKEY or WATSONX_API_KEY`. Add `GRANITE_MODEL_ID=` to the root `.env.example`. Remove `casewriter/.env.example` (one env file for the whole stack).
- **A3. Shared Redis connection.** Create `common/bus.py`:
  ```python
  import os, redis
  TXNS, SCORED, RINGS, CASES, DECISIONS = "txns", "scored", "rings", "cases", "decisions"
  def connect():
      return redis.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         decode_responses=True)
  ```
  Case writer uses it. Replayer, scorer, rings and page should use the same file so no service can drift on host name or stream names. For laptop testing, set `REDIS_HOST=localhost` in the shell.
- **A4. Libraries.** Add `requests` to root `requirements.txt`, or replace the HTTPS call with `ibm-watsonx-ai` (already listed, and the guide's first choice). Add a `## Libraries` section to the root README linking redis-py and whichever HTTP client we keep. Delete `casewriter/requirements.txt`.

Done when: `docker compose up -d --build casewriter` shows the container staying up and logging `case writer listening on 'rings'`.

### Phase B: make it behave under a live replay

Do this before the hour-8 end-to-end test. About 2 hours.

- **B1. Coalesce versions per batch.** After `XREADGROUP` returns up to 10 messages, keep only the newest version per `ring_id`; ack the older ones without processing.
- **B2. Skip Granite when nothing changed.** Keep an in-memory dict `last_facts[ring_id]`. If the new facts pack equals the old one except `last_seen`, `duration_hours` and `version`, re-emit the previous summary with the new version and do not call Granite. Record `check_note = "reused vN summary"`.
- **B3. Two-phase emit.** Write the template case the instant a ring arrives (`source=template`, `check_note="granite pending"`). Then call Granite. If the check passes, write a second case for the same `ring_id` and `version` with `source=granite`. The page always has text within milliseconds and upgrades it seconds later. This also gives the video a visible "Granite summary arrived" moment.
- **B4. Granite call hygiene.** `TIMEOUT_SECONDS = 15` for the chat call. Cache the IAM token with its expiry and refresh at 50 minutes. On a 401 or 404, log once at startup level, set `granite_disabled = True`, and stop retrying; every case then says `check_note = "granite disabled: 404 model_not_supported"` so the problem is visible on the page.
- **B5. Drain pending on startup.** Before the main loop, call `XREADGROUP` with id `0` once and process whatever comes back, then switch to `>`. Pass `--consumer` or use `socket.gethostname()` as now; both work.
- **B6. Timestamps.** `written_at` in UTC (`datetime.utcnow()`). Add `ring_last_seen` (data time, copied from the ring) and `written_after_s` (wall-clock seconds between the ring message's Redis ID timestamp and the case write) so Pair 1 can report both time-to-alert in data time and case-writer latency.
- **B7. Second pass-on duration.** Add `hours_to_pass_on_all` = first incoming to last outgoing from the key account. Keep `hours_to_pass_on` as is. The template should say "began passing it on within {hours_to_pass_on} hours and had moved {passed_on_pct}% within {hours_to_pass_on_all} hours". Both numbers are then in the facts pack and pass the fact check.

Done when: `--push-fake` with 30 copies of the same ring at increasing versions produces at most a handful of Granite calls, and `docker compose restart casewriter` mid-replay loses no rings.

### Phase C: make the page's job trivial

Agree with Pair 3 before they start the page. About 30 minutes of case-writer work.

- **C1. Latest-case hash.** In addition to `XADD cases`, write `HSET case:<ring_id>` with the same fields. The page does one `HGETALL case:R-0042` per ring instead of scanning the stream and picking the newest version. The stream stays as the audit trail. Set no expiry; the dataset is 10 days.
- **C2. Live counters.** `HSET stats:casewriter cases_written N granite_tried N granite_passed N`. The top bar reads it to show "Granite passed the fact check in X of Y cases" live.
- **C3. Page rules to write down.** Show the five summary parts, then a small label from `source` ("Granite, fact-checked" or "Template") with `check_note` as a tooltip. When `case:<ring_id>` does not exist yet, render "summary coming". Optionally show `facts` numbers next to the ring picture.

### Phase D: align with preprocessing and the ring service

- **D1. Exchange rates.** `make_rates.py` implements cleaning rule 4. Move it to `scripts/make_rates.py` and make it the team's only implementation, writing `~/shared/data/rates.json`. Pair 1's cleaning notebook reads that file instead of recomputing. Then the test rings and the scorer agree on every dollar amount.
- **D2. Test tooling paths.** Point `patterns_to_rings.py` at `/data/HI-Small_Patterns.txt` and `/data/rates.json` by default (the compose mount), with the current arguments as overrides. Keep `--push-fake` as this step's "5 fake messages" script from the guide's Test-each-step-alone section.
- **D3. Shapes.** Add `"other": "money moved between a connected group of accounts in no single recognised pattern"` to `SHAPE_TEXT`, and fix the article in `shape_label` so unknown shapes read "an unusual pattern".
- **D4. Confirm with Pair 2 (ring service):** edges as dicts or lists; `shape` values exactly from the guide's table (Fan-out, Fan-in, Scatter-gather, Gather-scatter, Cycle, Bipartite, Stack, Other); `version` increments on every republish; `accounts` excludes hub accounts; `xgb_score` is the mean of edge scores and `gnn_score` may be empty until the GNN runs. The case writer already tolerates an empty `gnn_score`.
- **D5. Stream memory (not casewriter, but on the same Redis).** Replayer and scorer should `XADD ... MAXLEN ~ 200000` on `txns` and `scored`. A 5-million-row replay without a cap will fill the VM's memory. `rings`, `cases` and `decisions` are small and need no cap.

### Phase E: Granite connection

- **E1. Hour 0:** finish the mandatory "Predictive AI + Generative AI" lab, note exactly how it connects (SDK or REST, which model ID, which region URL). If it differs from `granite.py`, replace only the body of `call_granite()`.
- **E2.** Confirm the model ID in the watsonx project. Candidates change; do not trust `ibm/granite-3-8b-instruct`.
- **E3.** Run `casewriter.py --file fake_rings.json` with real credentials. Expect `source: granite` on at least 2 of 3. If the fact check rejects every reply, lower temperature is already 0, so widen rounding tolerance slightly or shorten the facts pack before touching the prompt.

---

## 4. Contracts with neighbouring steps

### In: `rings` stream (written by the ring service, step 4)

Fields, all text; lists as JSON:

```
ring_id, version, shape, n_accounts, accounts, edges, xgb_score, gnn_score, reasons, first_seen, last_seen
edges: list of {txn_id, from, to, amount_usd, ts, score}   or   [txn_id, from, to, amount_usd, ts, score]
ts and first_seen/last_seen: 2022-09-08T14:00:00
account IDs: 10_8000EBD30
```

Case writer consumer group: `casewriter`. Multiple case-writer containers can share it.

### Out: `cases` stream and `case:<ring_id>` hash (read by the page, step 6)

```
ring_id, version, summary (JSON: summary, what_happened, why_suspicious, possible_victim, suggested_next_step),
source (granite | template), check_note, written_at (UTC ISO), facts (JSON), ring_last_seen, written_after_s
```

A ring may get two `cases` entries for the same version (template first, Granite second). The hash always holds the latest.

### Stats: `stats:casewriter` hash

```
cases_written, granite_tried, granite_passed
```

---

## 5. What we need from preprocessing before integrating

The case writer never reads the raw files, so the dependency is small. We are waiting so that one person can watch a transfer travel replayer -> scorer -> rings -> casewriter -> page and fix all the seams at once.

Needed:

- `~/shared/data/clean.csv` and `splits.json` exist, so the replayer can stream the test part.
- `~/shared/data/rates.json` produced by the shared script (D1), so test rings use the same dollar amounts as the scorer.
- The account ID rule applied in `clean.csv` exactly as `bank_without_leading_zeros + "_" + account`. The fact check's account regex (`\d+_[0-9A-Za-z]+`) depends on it.
- Pair 1's two or three chosen demo attempts (one scatter-gather, one fan-out) with their start times, so `--start` on the replayer and the case-writer demo moments match.
- Redis running in compose with no published port, and `common/bus.py` (A3) in place.

Not needed from preprocessing: the feature table, the model, or the GNN. The case writer works with empty scores.

---

## 6. Integration-day runbook

Work through these in order. Each step has a check; do not move on until it passes.

1. **Phase A changes merged.** `docker compose up -d --build` then `docker compose ps`: casewriter is Up.
2. **Fake rings through the real stack.** From the VM: `docker compose exec casewriter python -m services.casewriter.main --push-fake services/casewriter/fake_rings.json`. Then `redis-cli XRANGE cases - +` inside the redis container shows three cases. `HGETALL case:R-0001` returns the latest.
3. **Page reads them.** Pair 3's page shows three rings with summaries and the source label. Missing hash shows "summary coming".
4. **Real rings.** Start the replayer with `--start` a few data-hours before the first demo attempt. Watch `docker compose logs -f rings casewriter`. A case appears within a few seconds of each ring.
5. **Version churn test.** Let the replay run 5 real minutes. `HLEN stats:casewriter` and the log line show Granite calls well below the number of ring versions (B2 working).
6. **Restart test.** `docker compose restart casewriter` mid-replay. No `rings` message is left pending: `redis-cli XPENDING rings casewriter` shows 0 after it comes back.
7. **Granite live.** With real credentials in `.env`, at least some cases show `source=granite`. Record the pass rate for the pitch.
8. **Deliberate lie.** Run `test_casewriter.py` on the VM and screenshot `test_lying_granite_reply_falls_back` passing, for the video.
9. **Screenshots.** `docker compose ps`, the `cases` XRANGE, and the page with a Granite-labelled summary, into `screenshots/`.

---

## 7. Decisions and why

- **Wait for preprocessing before wiring Redis.** Integration problems in a stream pipeline are easiest to find when you can push one message in at the front and watch it come out the back. Doing the case writer alone would mean inventing fake `rings` messages that may not match what the ring service really sends.
- **Template first, Granite second (B3).** The guide says "a case always has a correct summary". Writing the template immediately satisfies that under any Granite latency, and the upgrade is a better demo than a delay.
- **Hash plus stream (C1).** The page should not have to reconstruct "latest per ring" from a stream. The hash is the read model; the stream is the log.
- **Shared `common/bus.py` (A3).** Three of the twelve problems above are "different service, different Redis host or stream name". One file removes that class of bug for the pairs who have not started.
- **Keep `requests` or switch to the SDK (A4).** Either is fine. The deciding factor is what the mandatory lab uses; copy its connection method.
