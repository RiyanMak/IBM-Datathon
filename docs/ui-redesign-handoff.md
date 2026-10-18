# Investigation workspace redesign

## Direction

A light, composed workspace: cool off-white canvas, white surfaces, one cobalt accent for selection
and primary actions, amber for "requires review", coral for the inferred origin account and green for
the inferred final recipient. The product is organised as a landing overview plus one page per
investigation step, with a step rail and a case context bar carrying the selected case across pages.
Motion explains change rather than decorating: reveal-on-scroll on the overview, a short page-enter
transition, hover/press states, and a slow traveling pulse on the hero diagram. Everything is static
under `prefers-reduced-motion`.

## Main files

- `.streamlit/config.toml`: pinned light palette for both theme variants, minimal toolbar.
- `services/page/app.py`: entry; `st.navigation(position="top")` over `services/page/views/`.
- `services/page/ui.py`: shared snapshot/theme helpers, case bar, step rail, status pills, motion frame.
- `services/page/views/home.py`: hero, live status strip, pipeline, chapter cards.
- `services/page/views/cases.py`: queue with filters, card-style options, selected-case preview.
- `services/page/views/investigate.py`: graph, inspector, transfer list, step links.
- `services/page/views/evidence.py`: reasons, scores, observations, explanation and its history.
- `services/page/views/decide.py`: decision form outside polling fragments, decision history.
- `services/page/views/status.py`: connection, windows, counters, retry.
- `services/page/styles.css`, `motion.js`, `assets/`: tokens, composition, motion, brand marks.
- `services/page/workspace.py`: `roles()` infers origin / final recipient for the graph.
- `services/page/flow_component/`: role-aware nodes, new legend, new palette.
- `Dockerfile`: copies `.streamlit/` so the container uses the same theme.

## Verification record (2026-10-17)

- `pytest -q services/page services/casewriter`: **103 passed** (AppTest suite ported to pages).
- `node --check` on the graph component and the motion script: passed. `git diff --check`: passed.
- Fixture launcher (`python -m services.page.fake.run`) inspected in Chrome at 1440×900 and 390×844:
  overview with reveal-on-scroll, queue, investigate (origin/final recipient marked), evidence, and
  a real decision recorded (flash, status pill, history) on the decide page.

## Known limits

- Fixture mode only; no Granite call, replay progress or bank enforcement. Scores are fixture inputs.
- The top navigation collapses into a menu below roughly 850 px; the graph needs horizontal scroll there.
- Theme switching from the Streamlit menu shows the same light palette by design.
