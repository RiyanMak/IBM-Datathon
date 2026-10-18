# Investigation workspace redesign

## Direction and implementation

A calm, neutral workspace with teal selection and amber review cues. The principal interaction
links case selection, canonical account/transfer selection, a directional graph, and the transfer
inspector. The default first case is the highest-scored case. Runtime diagnostics are secondary.

Main files:

- `services/page/app.py`: Streamlit layout, stable queue, explicit case updates, evidence, history,
  and a non-polling decision form.
- `services/page/styles.css`: semantic colors, layout, focus, responsive and reduced-motion rules.
- `services/page/workspace.py`: identity, graph layout/scope, pagination helpers and honest labels.
- `services/page/flow_component/`: local SVG graph with mouse/keyboard selection, zoom and fit.
- `services/page/models.py`, `state.py`: optional native currencies, explicit fact-check status,
  fixture metadata, message freshness and decision history.
- `services/page/decisions.py`: optional idempotency side keys; stream fields unchanged.
- `services/page/fake/run.py`: isolated laptop launcher; no Docker or existing Redis required.
- `requirements-page.txt`: page-only laptop dependencies, independent of the full ML stack.

## Discovery and integration truth

Inspected README, deployment manifests, page plans, all page modules/tests, fixture feed,
case-writer generation/fact checking, and stream contracts. Working tree was clean before edits.
No applicable AGENTS.md files were present in the repository or its parent directories.

The existing application could not be run before implementation: Streamlit and Redis packages
were absent, PyPI resolution was blocked, and Docker was stopped. This is a source audit, not a
completed visual audit. The scorer/rings services contain placeholders; the Docker base image
is still a template. The repository's fixture cases exercise the frontend contracts, not a
completed detection pipeline. Granite was not called during this work.

## Verification record

Executed in this environment:

- `pytest -q services/page services/casewriter`: **36 passed, 5 skipped**. Skipped coverage requires
  unavailable Streamlit/Redis/fakeredis dependencies; this is not a passing end-to-end result.
- Python compilation for the changed modules, launcher, preview generator and AppTest suite: passed.
- `node --check services/page/flow_component/main.js`: passed.
- `git diff --check`: passed.
- A file-openable harness was generated from the actual graph component and repository fixtures.

Attempts blocked by the environment:

- Installing Streamlit/Redis from PyPI: DNS/network unavailable. Offline cache lacked those packages.
- Binding a localhost HTTP server: `PermissionError: Operation not permitted`.
- Chrome inspection through computer use: `Computer Use permissions are not granted`.

Therefore no rendered screenshots, viewport checks, browser interactions, or running application
are claimed. The added AppTest and idempotency regressions still need execution with the listed
page dependencies. The user was given the fresh-instance launcher command.

## Runtime acceptance pass still required

1. Launch the real page with `python -m services.page.fake.run` in a normal terminal.
2. Inspect 1440px, 1280px and 390px widths, plus text zoom and long identifiers.
3. Select R-0001; click/keyboard-select an edge and account, confirm matching details and transfers.
4. Review reasons, template provenance, missing native currency and missing-score states.
5. Record each decision action; confirm one persisted row, reviewed version, note and history.
6. Inject a new case version during selection; verify explicit update, stable positions/selection,
   stale-summary warning, and unchanged draft note. Stop/restart the connection and test retries.
7. Check empty/filter-empty states, a self-transfer, disconnected nodes and graph scope limits.
8. Check dark theme, keyboard focus, reduced motion and tab/focus stability during polling.

The CSS includes responsive rules and contrast-oriented tokens, but WCAG compliance and browser
layout quality are not certified by source inspection. No framework migration or scoring changes
were made. The current graph uses bounded deterministic layouts; dense real cases may need further
layout refinement after visual inspection. Changes remain uncommitted for review.
