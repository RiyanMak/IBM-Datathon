# Investigator page: build plan

Status as of 2026-10-17. Step 6 of the team guide (the page where a person decides). Nothing in `services/page/` exists yet beyond a placeholder. This document is the plan agreed before writing code; it covers:

1. What the page must do (from the team guide)
2. Stack choice and why
3. Module layout
4. How the page reads Redis
5. Staying flexible while the LLM side is still changing
6. Keeping the fake parts separate
7. Things to keep note of
8. Build order
9. Contracts with neighbouring steps
10. Open decisions

The team guide (`Mule Ring Hunter: Team Helper Guide`) is the source of truth for stream names and fields. Where this document and the guide disagree, the guide wins and this document should be fixed.

---

## 1. What the page must do

From the guide, step 6:

- **In:** the `scored`, `rings` and `cases` streams. Read-only, no consumer group.
- **Out:** the `decisions` stream. Fields: `ring_id`, `decision` (`freeze`, `dismiss` or `more_info`), `note`, `decided_at`.
- **Areas on screen:**

| Area | Content | Read from |
|---|---|---|
| Top bar | Transfers scored, transfers flagged, open rings, p50 and p99 latency, updating live | `scored` |
| Ring list (left) | Newest and highest-scoring rings: ID, shape, number of accounts, XGBoost and GNN scores, first seen | `rings` |
| Ring picture (right) | Accounts as dots, transfers as arrows labelled with amounts; key account and collector highlighted | `rings` (+ `facts` from `cases`) |
| Why | The top 3 reasons as plain phrases | `rings` |
| Summary | The case summary, with a small label: written by Granite (fact-checked) or by template | `cases` |
| Decision | Buttons: Freeze accounts, Dismiss, Needs more info; a note box | writes to `decisions` |

- **Hard rules:** never crash on an empty stream (start of a replay) or a missing case (show "summary coming"); refresh about every 2 seconds; port 8501 bound to `127.0.0.1` only and reached through an SSH tunnel.
- **Done when:** during a replay the counters move, a demo ring appears with its picture, reasons and summary, and clicking Freeze writes a decision.

Already in place: `docker-compose.yml` runs `streamlit run services/page/app.py --server.port 8501 --server.address 0.0.0.0` with the port published on `127.0.0.1:8501` only; `streamlit`, `flask`, `graphviz` and `redis` are in the root `requirements.txt`; `common/bus.py` gives the Redis connection and stream names.

---

## 2. Stack choice

**Streamlit for the UI, with a framework-free core underneath.**

Why Streamlit: the guide names it, the compose file already runs it, and it is the fastest route to a working demo. `st.graphviz_chart` renders in the browser, so the image needs only the `graphviz` Python package, not the system binary.

The risk: Streamlit depends on `pyarrow`, which may have no s390x wheel. The guide says to check at hour 0 and switch to Flask if `pip install streamlit` fails inside the Jupyter image.

So the rule is: **every piece of logic lives in plain Python modules that import nothing from Streamlit.** `app.py` is a thin view. If the hour-0 check fails, the fallback is Flask with one JSON route (`/api/state`, which just serialises the same `PageState`) and one HTML page that polls it every 2 seconds. Only the view gets rewritten.

Decision rule at hour 0, in this order:

1. `pip install streamlit` works in the Jupyter image and `streamlit --version` is 1.37 or newer: Streamlit, with fragments for live refresh.
2. Streamlit installs but is older than 1.37: Streamlit, with `time.sleep(2); st.rerun()` at the end of the script (the guide's method). Decision form must still be a `st.form`.
3. Streamlit does not install: Flask fallback (section 8, step 7).

---

## 3. Module layout

```
services/page/
  __init__.py
  models.py        dataclasses: Counters, RingView, CaseView, DecisionView, PageState   (no Streamlit)
  state.py         reads the streams, builds one PageState snapshot                     (no Streamlit)
  graph.py         ring -> Graphviz DOT text; key account and collector highlighted      (no Streamlit)
  decisions.py     writes a decision; reads the latest decision per ring                (no Streamlit)
  app.py           the Streamlit page; thin; the ONLY file that imports streamlit
  test_page.py     tests for state, graph and decisions, using fakeredis
  fake/            everything that only exists for testing alone (section 6)
    __init__.py
    feed.py        pushes fake scored / rings / cases / decisions messages onto a Redis
    test_feed.py   checks for the feed; deleted together with the folder
  README.md        how to run it alone, how to run it in compose, what the labels mean
```

Rules:

- `models.py`, `state.py`, `graph.py` and `decisions.py` are pure functions over a redis client and plain dicts. They are unit-testable with `fakeredis` and reusable by the Flask fallback unchanged.
- `app.py` holds no business logic. It calls `state.load(r)`, `graph.dot(ring, facts)`, `decisions.write(r, ...)` and lays widgets out.
- Nothing outside `fake/` imports from `fake/`. See section 6.

---

## 4. How the page reads Redis

All reads go through `common.bus.connect()`. The page never uses a consumer group and never acknowledges anything.

### `rings`

`XREVRANGE rings + - COUNT 200`. Keep the highest `version` per `ring_id`. Offer two orderings: newest first (by `last_seen`) and highest score first (by `xgb_score`). Tolerate an empty `gnn_score` (the GNN may not be running); show it as a dash, not 0.

### `cases`

`XREVRANGE cases + - COUNT 500`. Pick the latest case per `ring_id`. Ordering rule: highest `version` wins; at equal version a `granite` case beats a `template` one, because the case writer plans to emit the template first and the Granite rewrite a few seconds later (its plan, B3).

The case writer's plan also proposes a `case:<ring_id>` hash (its Phase C, C1) so the page can do one `HGETALL` per ring. **That hash is not implemented yet**, so the page must work from the stream alone. `state.latest_case(r, ring_id)` is the one function to change when the hash lands.

### `scored`

Totals from `XLEN scored` (and the `flagged` count from the last N messages scaled is not good enough; keep a running count from the messages actually read, see note below). Latency from the last 500 messages: `scored_at - sent_at`, p50 and p99 over that window. **Never `XRANGE scored - +`**; it will hold hundreds of thousands of rows.

Counting flagged transfers exactly needs either every message or a counter the scorer maintains. Plan: ask the scorer to `HINCRBY stats:scorer scored 1` and `flagged 1` as it goes (one line in their loop). Until that exists, the page shows flagged as "N in the last 500 scored" and labels it so.

### `decisions`

`XREVRANGE decisions + - COUNT 500`, mapped to the latest decision per `ring_id`. Used for the "Frozen / Dismissed / More info" badge in the list and for "open rings" in the top bar, which means rings with no decision yet.

### Robustness

- Every `json.loads` and every float conversion sits inside `try/except`. A malformed message is skipped and logged once. The page never raises on bad data.
- Missing streams and empty streams are the same as zero messages.
- Redis connection errors render a single banner ("waiting for Redis") and retry on the next refresh.

---

## 5. Staying flexible while the LLM side is still changing

The case writer is still being worked on, and more LLM output may follow. These choices keep the page from having to change each time.

- **Render summary parts from the data, not from code.** A case's `summary` is a JSON object with five parts today (`summary`, `what_happened`, `why_suspicious`, `possible_victim`, `suggested_next_step`). The page iterates over the keys in the order they arrive, with a label map for the known ones and a title-cased fallback (`draft_report` becomes "Draft report") for anything new. A sixth part appears with no page change.
- **Source badge from a registry.** A small dict maps `source` values to labels: `granite` to "Granite, fact-checked", `template` to "Template". An unknown source shows its raw value. `check_note` is always available on hover or in an expander, so a disabled or failing Granite is visible rather than hidden.
- **Upgrade moment.** When a ring's latest case flips from `template` to `granite` between two refreshes, the summary panel highlights briefly and a toast says "Granite summary arrived". This is the visible moment the case writer plan wants for the video.
- **The pitch number.** "Granite passed the fact check in X of Y cases" reads the `stats:casewriter` hash when it exists (case writer plan, C2) and otherwise computes X and Y from the cases already read. Same widget either way.
- **Optional facts panel.** If a case carries the extra `facts` field, a small table shows the numbers (passed-on %, hours to pass on, key account, collector) beside the picture, and `graph.py` uses `key_account` and `collector` to highlight nodes. If `facts` is absent, the panel is skipped and nothing is highlighted.
- **Pluggable readers.** `state.load()` is built from a list of readers, one per source (`rings`, `cases`, `scored`, `decisions`). A future LLM output on a new stream or hash (a per-ring Q&A, a draft report, a second model's summary) is one new reader plus one new panel. Nothing else moves.
- **Version history on demand.** The `cases` stream is the audit trail. An expander per ring can list every case version with its source and `check_note`. Useful for debugging the case writer live and for the "how do you stop the AI making things up" question.

---

## 6. Keeping the fake parts separate

Goal: when the real replayer, scorer and ring service are running, removing every fake input should be a folder delete and a short checklist, not a hunt through the code.

### Rules

1. **One folder.** Everything that exists only to run the page without the rest of the stack lives in `services/page/fake/`. Nothing in `app.py`, `state.py`, `graph.py` or `decisions.py` imports from it, references its files, or has a "demo mode" flag that depends on it. The page cannot tell fake messages from real ones because they arrive on the same streams in the same format.
2. **Fake data enters through Redis only.** `fake/feed.py` writes to the real stream names from `common.bus` with the exact field sets in the guide's Handoffs table. The page never reads a JSON file directly. This is what makes the fake feed a true stand-in: if the page works on the fake feed, the only remaining integration risk is the neighbours not matching the contract.
3. **Reuse the case writer's fakes rather than inventing new rings.** `fake/feed.py` pushes `services/casewriter/fake_rings.json` onto `rings` (the case writer's `--push-fake` already does exactly this; the feed calls that function rather than copying it), and generates `cases` rows by running the case writer's `--file` mode and pushing the output. That keeps one source of truth for what a ring looks like. The only new data the page owns is generated in code: fake `scored` rows (for the counters) and two fake `decisions` (to test badges). No fixture files are needed.
4. **Fake content is visibly fake.** Fake ring IDs stay in the `R-000x` range from `fake_rings.json` (or `R-9xxxx` from `patterns_to_rings.py`); fake scored rows use `txn_id` values prefixed `FAKE-` and accounts like `42_FAKE1A2B3`; fake decision notes start with `FAKE`. A fake Granite upgrade (`--fake-upgrade`) keeps the template text and says so in `check_note`. Real IDs from the cleaning pipeline are plain integers, so a stray fake row is obvious in a screenshot.
5. **Tests use `fakeredis`, not the fake feed.** `test_page.py` builds its own small messages in memory. The fake feed is a developer tool for looking at the page, not a test dependency. Deleting `fake/` must not break `pytest`.
6. **The name says so.** The folder is `fake/`, matching `fake_rings.json` and `--push-fake` in the case writer, so a search for `fake` finds every stand-in in the repo.

### Inventory of fake and test-only parts across the repo (as of today)

| Location | What | Owner | Keep after integration? |
|---|---|---|---|
| `services/casewriter/fake_rings.json` | 3 hand-made rings | Case writer | Yes, the tests use it; it is also the guide's "5 fake messages" script for that step |
| `services/casewriter/casewriter.py --push-fake` | Pushes those rings onto `rings` | Case writer | Yes, debugging tool |
| `services/casewriter/patterns_to_rings.py` | Real pattern attempts turned into rings | Case writer | Yes until the ring service is proven; test input only |
| `services/page/fake/` | Fake `scored`, `cases`, `decisions` feed (rings come from the case writer's file) | Page | Remove or archive once the real stack runs end to end |
| `scripts/clean_data.py --sample-every` | Sample runs of the cleaning pipeline | Cleaning | Yes, documented as not final |

### Cleanup checklist for integration day

- [ ] Real replayer, scorer and ring service are writing to `txns`, `scored` and `rings` in compose.
- [ ] `FLUSHALL` on the compose Redis (or `docker compose down -v`) so no fake rows remain on any stream.
- [ ] `grep -rn "FAKE-\|R-000" services/page/ --include=*.py` returns only hits inside `services/page/fake/`.
- [ ] Decide: delete `services/page/fake/` or keep it with a one-line README note saying it is a stand-in. Either way the page code does not change.
- [ ] Screenshots in `screenshots/` come from the real replay, not the fake feed.

---

## 7. Things to keep note of

1. **Streamlit reruns the whole script on every click.** Put the note box and the three buttons in one `st.form` so typing is not lost by a refresh. Use `st.fragment(run_every="2s")` for the live areas (top bar, ring list, summary) so only they refresh, not the form. Needs Streamlit 1.37 or newer; confirm at hour 0 with the pyarrow check.
2. **Two kinds of time.** `first_seen`, `last_seen` and edge `ts` are data time (September 2022). `written_at`, `scored_at`, `sent_at` and `decided_at` are wall-clock time. Label each on screen so the video narration does not trip. The case writer still writes local time, not UTC (its plan, B6); the page should not assume either and should not compute anything across the two kinds.
3. **Add `version` to decisions.** The guide lists four fields. Add `version` as an extra fifth field (the same way the case writer added `facts` to `cases`) so the audit trail says which version of the ring the person saw. Tell the team; anyone reading `decisions` can ignore it.
4. **Latency on the top bar** is `scored_at - sent_at` from the scorer, which is the guide's definition. The page does not add its own read latency to it. Show it as milliseconds.
5. **Testing on a Windows laptop.** A local Redis is needed: Docker Desktop (`docker run -p 6379:6379 redis:7`) or WSL. Run the page with `REDIS_HOST=localhost`. Streamlit and pyarrow install fine on x86, so the laptop proves the logic and the VM proves the platform.
6. **Dockerfile base image** is still `<linuxone-jupyter-image>:<tag>`. Not a page task, but nothing runs in compose until it is filled in at hour 0.
7. **Stream caps.** The replayer and scorer should write `txns` and `scored` with `MAXLEN ~ 200000` (case writer plan, D5). Once they do, `XLEN scored` undercounts the true total. For the demo that is acceptable; the top-bar label should say "recent" or the scorer should keep the `stats:scorer` counter (section 4).
8. **Security points the page carries** for the judging table: port bound to `127.0.0.1`, SSH tunnel to view, container runs as `jovyan`, no secrets needed by the page at all (it has no `.env`).
9. **No pandas in the page.** Keep the hot path to redis-py and plain dicts. Fewer moving parts on s390x and no accidental `st.dataframe` dependence on pyarrow if the Flask fallback is ever needed.

---

## 8. Build order

Each step has a check; do not move on until it passes.

1. **Fake feed and fixtures** (`services/page/fake/`). Check: `python -m services.page.fake.feed` against a local Redis, then `redis-cli XLEN scored`, `XLEN rings`, `XLEN cases` are all non-zero and `XRANGE cases - +` shows five-part summaries.
2. **Models and state layer** with tests on fakeredis. Check: `pytest -q services/page` passes; `state.load()` on an empty Redis returns an empty `PageState` without raising.
3. **Graph module** with tests on the three fake shapes. Check: DOT text for the scatter-gather ring has nine nodes, the key account styled differently, and amounts formatted as `$10,000`.
4. **Minimal Streamlit page:** list, picture, reasons, summary with source label, decision form. Check: a Freeze click produces a `decisions` row with `ring_id`, `version`, `decision`, `note`, `decided_at`, and the ring shows a Frozen badge on the next refresh. This is the first demoable point.
5. **Top bar** counters and latency. Check: numbers move while the fake feed pushes more scored rows.
6. **Polish:** upgrade highlight, Granite pass-rate widget, decided badges, filters (shape, minimum score, undecided only), version history expander.
7. **Flask fallback** only if the hour-0 check fails: `flask_app.py` serialising `PageState` to JSON plus `templates/index.html` and a small `static/app.js` that polls every 2 seconds and draws the ring with a vendored viz.js. No change to `state.py`, `graph.py` or `decisions.py`.
8. **In compose on the VM:** `docker compose up -d --build page`, tunnel with `ssh -L 8501:localhost:8501`, run the case writer's `--push-fake`, confirm three rings with "summary coming" turning into summaries. Then the cleanup checklist in section 6 before the real replay.

---

## 9. Contracts with neighbouring steps

### In: `scored` (from the scorer, step 3)

```
everything in txns (txn_id, ts, from_acct, to_acct, amount_usd, amt_paid, ccy_paid, amt_recv, ccy_recv, fmt, sent_at)
plus score, flagged (0 or 1), reasons (JSON list of 3 phrases), clues (JSON object), scored_at
```

Asked of the scorer: `HINCRBY stats:scorer scored 1` and `flagged 1` per message, so the top bar can show exact totals under a stream cap. Optional; the page works without it.

### In: `rings` (from the ring service, step 4)

```
ring_id, version, shape, n_accounts, accounts (JSON list), edges (JSON list of
{txn_id, from, to, amount_usd, ts, score} or lists in that order), xgb_score, gnn_score (may be empty),
reasons (JSON list), first_seen, last_seen
```

### In: `cases` (from the case writer, step 5)

```
ring_id, version, summary (JSON object: summary, what_happened, why_suspicious, possible_victim, suggested_next_step),
source (granite | template), check_note, written_at, facts (JSON, extra, optional)
```

A ring may get two cases for the same version (template first, Granite second). Later: `case:<ring_id>` hash and `stats:casewriter` hash, both optional for the page.

### Out: `decisions` (to the audit log)

```
ring_id, decision (freeze | dismiss | more_info), note, decided_at (UTC ISO), version (extra)
```

---

## 10. Open decisions

Agreed so far: Streamlit first with the Flask fallback designed in; streams-only reads now with the `case:<ring_id>` hash as a later one-function swap; `version` added to `decisions`.

Still to settle:

- Whether to ask the scorer for the `stats:scorer` counter now or accept "recent" counts on the top bar.
- Whether `services/page/fake/` is deleted or kept after integration (section 6 checklist).
- Which two or three demo rings Pair 1 picks, so a "pin to top" filter can be added if the list gets long during the replay.
