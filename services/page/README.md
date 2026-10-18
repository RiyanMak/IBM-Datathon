# Step 6: Investigator page

**Reads:** Redis streams `scored`, `rings`, `cases`, `decisions` (plus `stats:scorer` and `stats:casewriter` if they exist) · **Writes:** `decisions`

The page where a person decides. It lists flagged cases from Redis, draws their transfers, shows the supplied reasons and explanation, and records a freeze decision, a request for more information, or a dismissal with a note. Decisions are audit records; they do not enforce bank actions.

Design and reasons: `docs/investigator-page-plan.md`. Known limits and ideas: `docs/page-improvements.md`.

## Files

| File | What it does | Imports Streamlit? |
|---|---|---|
| `models.py` | The views the page draws, plus label tables (unknown shapes, sources and summary parts get readable labels) | No |
| `state.py` | Reads the streams into one snapshot; never raises; filters, upgrade detection, case history | No |
| `graph.py` | Retained Graphviz DOT export, roles highlighted from the case facts | No |
| `workspace.py` | Stable selection, graph layout and display scope, presentation helpers | No |
| `flow_component/` | Local SVG graph with linked mouse/keyboard selection, zoom and fit | No |
| `decisions.py` | Validates and writes a decision | No |
| `fmt.py` | Display text: numbers, times, top-bar tiles, facts rows, Markdown escaping | No |
| `app.py` | Entry point: theme frame, brand mark, top navigation | Yes |
| `ui.py` | Shared Streamlit helpers: snapshot, theme, case bar, step rail, motion | Yes |
| `views/` | One script per page: overview, cases, investigate, evidence, decide, status | Yes |
| `styles.css`, `motion.js`, `assets/` | Semantic tokens, reveal/page transitions, brand SVGs | No |
| `test_*.py` | Tests; they build their own messages and never use `fake/` | |
| `fake/` | Fake feed for running the page alone. Delete once the real stack runs | |

## Run it on a laptop

From the repo root, with Docker Desktop running (PowerShell):

```
docker run -d --name page-redis -p 6379:6379 redis:7
$env:REDIS_HOST = "localhost"
.venv\Scripts\python -m services.page.fake.feed --decisions
.venv\Scripts\streamlit run services/page/app.py --client.toolbarMode minimal
```

Open http://localhost:8501. For moving counters and a Granite upgrade to watch, in a second terminal:

```
$env:REDIS_HOST = "localhost"
.venv\Scripts\python -m services.page.fake.feed --scored 0 --fake-upgrade 30 --live
```

Start over with `python -m services.page.fake.feed --reset --decisions` (it refuses if it finds real data) or `docker rm -f page-redis`.

## Run it in the stack

`docker compose up -d --build page`, then from your laptop `ssh -L 8501:localhost:8501 linux1@<vm-ip>` and open http://localhost:8501. The port is bound to 127.0.0.1 on the VM; never publish it.

## Tests

```
.venv\Scripts\python -m pytest -q services/page
```

## Rules for anyone adding to the page

1. Logic goes in the modules without Streamlit; `app.py`, `ui.py` and `views/` only lay things out. That keeps the Flask fallback a view rewrite.
2. Every piece of data shown through `st.markdown`, captions, radio labels or badges goes through `fmt.md()`. Two dollar amounts in one line otherwise render as maths.
3. New data source: add a reader to `state.READERS` and a panel in `app.py`. Nothing else changes.
4. The decision form stays outside the live fragments, so a refresh never wipes what someone is typing.
5. Nothing outside `fake/` imports from `fake/`.

## Investigation workspace (UI refresh)

The application is one frame with one page per step, reached from the top navigation:
**Overview** (purpose, live status, pipeline), **Cases** (01), **Investigate** (02),
**Evidence** (03), **Decide** (04) and **System status** (diagnostics). The step rail and
the case context bar keep the selected case consistent across pages. The theme is pinned in
`.streamlit/config.toml` (light palette, cobalt accent, coral origin, green final recipient)
for every viewer; remove `[theme.dark]` there to let Streamlit's dark base and the page's own
dark tokens apply. Motion is limited to reveal-on-scroll on the overview, a short page
transition, and state transitions; all of it is disabled under `prefers-reduced-motion`.

The main path is **choose a case → select an account or transfer → review evidence → record a decision**.
The default queue sorts by model score and holds its order until **Update queue** is clicked.
A selected case holds its reviewed version until **Review updated case** is clicked. The decision
records that reviewed version, even if newer stream data arrives while the note is being entered.

### Fresh laptop instance without Docker

From the repository root, in a normal terminal with network access:

```bash
uv venv --python 3.12 .venv  # skip if the environment already exists
uv pip install --python .venv/bin/python -r requirements-page.txt
.venv/bin/python -m services.page.fake.run
```

Open **http://127.0.0.1:8501**. This starts a private, ephemeral Redis-compatible fixture server
on a free loopback port and the real Streamlit application. It seeds the existing three cases,
uses the real template case writer, and labels the page **Fixture simulation**. Decisions are
acknowledged by that server and appear in History, but are lost when the launcher exits.
There is no real Granite call, model scoring, replay progression, or bank enforcement in this mode.
Ctrl+C stops both processes. Use `--port 8503` if the page port is already occupied.
The fixture launcher uses [fakeredis TCP mode](https://fakeredis.readthedocs.io/en/latest/).

The existing external-Redis and compose entry points still use `app.py`. For an external Redis,
set `REDIS_HOST` and `REDIS_PORT`, then run Streamlit as before. `page:feed_mode=fixture` is optional
metadata written by the fake feed; remove this key when replacing fixtures with a clean synthetic
replay. The page's default label is **Synthetic-data simulation**. Do not use either label for
real customer data without first adding a trustworthy data-provenance contract.

### Interaction and evidence boundaries

- The local SVG component in `flow_component/` has no CDN, build step, or JavaScript dependency.
  Graph click/Enter/Space and the native inspection list share canonical selection keys. Short
  graph labels never become identity. The list also exposes transfers outside the graph limit.
- The graph marks the inferred **origin** (sends, never receives; the earliest sender in a pure
  cycle) in coral and the inferred **final recipient** (receives, never sends on) in green. Roles come
  from the drawn transfers only; self-transfers never define a role. The legend says they are inferred.
- The graph draws at most 36 accounts and 60 transfers, prioritizing a selected item and large
  transfers. The scope is always visible. It preserves positions and zoom within the current
  case; **Fit view** resets zoom. The transfer list is paginated at 12 rows.
- `workspace.py` contains presentation logic, independent of Streamlit. The old `graph.py` DOT
  builder remains available for consumers that need it; `app.py` uses the SVG component.
- USD amounts are estimates and transfer volume may count the same money multiple times. Native
  `amt_paid`, `ccy_paid`, `amt_recv`, and `ccy_recv` fields are displayed when supplied on dictionary
  edges. The original six-field edge arrays remain compatible. Missing values are unavailable.
- Explanations say **fact check passed** only for `source=granite` with `check_note=passed fact check`.
  Fake upgrades remain explicitly simulated. Stale facts do not populate current observations.
- Decision buttons record an audit instruction. They do not perform a bank freeze. Failed writes
  retain notes. An optional request ID adds an atomic, 24-hour Redis side key to deduplicate retries;
  the decision stream's fields and existing caller signatures remain compatible. Duplicate identical
  submissions are also suppressed within the page session.
- Diagnostics expose the bounded stream windows, connection freshness, and separate dataset times.
  No progress percentage is inferred when the replayer does not supply one.

### Checks and demo

```bash
.venv/bin/python -m pytest -q services/page services/casewriter
node --check services/page/flow_component/main.js
# Optional file-openable QA harness for the actual graph component:
.venv/bin/python -m services.page.fake.preview_graph --out /tmp/mrh-graph-preview.html
```

Start with **R-0001**, select its first transfer (or choose it in the inspection list), inspect
sender/recipient, dataset time, and estimated USD, then select the recipient account to see its
connections and transfer list. Open **Evidence & explanation**, compare the supplied reasons and
template explanation, enter a note, and click **Request more information**. Verify the confirmation
and the new record under **History**. Review **R-0003** for a cycle. Use the fixture marker when
explaining this demonstration; the scores are fixture inputs.

The standalone graph QA harness tests only component rendering. It does not verify Streamlit,
Redis persistence, page responsiveness, or the full user journey. See `docs/ui-redesign-handoff.md`
for the verification record and remaining work.
