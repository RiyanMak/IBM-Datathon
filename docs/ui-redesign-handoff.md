# Investigation workspace redesign

## Direction

An internal investigation tool in IBM's visual language (Carbon): IBM Plex Sans, square corners,
hairline grid lines, flat white/gray tiles, Blue 60 (#0F62FE) as the only interactive colour, amber
for "requires review", Carbon orange for the inferred origin account and Carbon green for the inferred
final recipient. Light (Gray 10) and dark (Gray 100) themes are both configured; a header button
switches between them. The entry page is a single leadspace: name, one-line purpose, the animated
origin-to-recipient flow, and one primary action into the queue. Explanatory material lives on a
separate "How it works" page so the working pages stay direct. Motion follows Carbon's productive
easing: reveal-on-scroll, a 240 ms page transition, hover/press states, and a slow traveling pulse
on the entry diagram. Everything is static under `prefers-reduced-motion`.

## Main files

- `.streamlit/config.toml`: Carbon light and dark themes, IBM Plex, square radii, viewer toolbar.
- `services/page/app.py`: entry; `st.navigation(position="top")` over `services/page/views/`.
- `services/page/ui.py`: shared snapshot/theme helpers, case bar, step rail, status pills, motion frame.
- `services/page/views/home.py`: entry leadspace with the flow animation and the Start button.
- `services/page/views/about.py`: How it works: pipeline, four steps, boundaries.
- `services/page/views/cases.py`: queue with filters, card-style options, selected-case preview.
- `services/page/views/investigate.py`: graph, inspector, transfer list, step links.
- `services/page/views/evidence.py`: reasons, scores, observations, explanation and its history.
- `services/page/views/decide.py`: decision form outside polling fragments, decision history.
- `services/page/views/status.py`: connection, windows, counters, retry.
- `services/page/styles.css`, `motion.js`, `assets/`: tokens, composition, motion, brand marks.
- `services/page/workspace.py`: `roles()` infers origin / final recipient for the graph.
- `services/page/flow_component/`: role-aware nodes, legend, Carbon palette, and the Play flow replay (pulses along transfers in dataset-time order; stepped highlight under reduced motion).
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
- The header sun/moon toggle writes Streamlit's per-page local-storage keys and reloads; it is per browser.
- The entry page's grid-and-glow background is CSS only, toggled by a body class from motion.js.
