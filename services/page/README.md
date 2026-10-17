# Step 6: Investigator page

**Reads:** Redis streams `scored`, `rings`, `cases`, `decisions` (plus `stats:scorer` and `stats:casewriter` if they exist) · **Writes:** `decisions`

The page where a person decides. It lists live rings, draws each one, shows why it was flagged and the case summary, and records Freeze, Needs more info or Dismiss with a note. The AI recommends and explains; the person decides, and every decision is logged.

Design and reasons: `docs/investigator-page-plan.md`. Known limits and ideas: `docs/page-improvements.md`.

## Files

| File | What it does | Imports Streamlit? |
|---|---|---|
| `models.py` | The views the page draws, plus label tables (unknown shapes, sources and summary parts get readable labels) | No |
| `state.py` | Reads the streams into one snapshot; never raises; filters, upgrade detection, case history | No |
| `graph.py` | A ring as Graphviz DOT text, roles highlighted from the case facts | No |
| `decisions.py` | Validates and writes a decision | No |
| `fmt.py` | Display text: numbers, times, top-bar tiles, facts rows, Markdown escaping | No |
| `app.py` | The Streamlit page. The only file that imports Streamlit | Yes |
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

1. Logic goes in the modules without Streamlit; `app.py` only lays things out. That keeps the Flask fallback a view rewrite.
2. Every piece of data shown through `st.markdown`, captions, radio labels or badges goes through `fmt.md()`. Two dollar amounts in one line otherwise render as maths.
3. New data source: add a reader to `state.READERS` and a panel in `app.py`. Nothing else changes.
4. The decision form stays outside the live fragments, so a refresh never wipes what someone is typing.
5. Nothing outside `fake/` imports from `fake/`.
