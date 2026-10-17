# Investigator page: things to improve

Ideas and known limits that are deliberately not done yet. Each one is safe to leave as it is for the demo. Pick them up when there is time, or when the real stack shows they matter. The build plan is in `investigator-page-plan.md`.

When you do one, move it to the "Done" section at the bottom with the date.

---

## Ring picture (`services/page/graph.py`)

- **Circular layout for cycle rings.** Cycle rings are drawn left to right, with one long arrow running back to the start. A circular layout (Graphviz `circo`) would show "money returns to where it began" more clearly. Change: in `build()`, when `ring.shape == "cycle"`, switch the layout engine (`layout=circo` in the graph attributes) and drop `rankdir`. Check that `st.graphviz_chart` and viz.js both honour `layout=circo`, and render R-0003 before and after.
- **Shape-specific layouts in general.** Fan-in might read better right to left, and bipartite or stack rings might read better with explicit ranks per layer. Only worth doing after seeing real rings from the ring service.
- **Large rings.** Above 60 transfers, only the largest 60 are drawn. A "show all" toggle or a layered summary may be needed if real rings get that big.

## Data layer (`services/page/state.py`, `models.py`)

- **A ring with one bad edge is skipped entirely.** Today a single malformed edge makes the whole ring message count as bad. An alternative is to drop only that edge and show the ring with a warning. Decide after seeing what the ring service actually sends.
- **Flagged count is approximate.** It is counted over the last 500 scored messages unless the scorer keeps `stats:scorer` (`HINCRBY stats:scorer scored 1`, `flagged 1`). Ask the scorer owner.
- **The Granite pass rate counts case messages, not rings.** It switches to the case writer's own numbers once `stats:casewriter` exists (case writer plan, C2).
- **`case:<ring_id>` hash.** When the case writer adds it (its plan, C1), change only `state.latest_case()` to read it.
- **`PageState.to_dict()` leaves out the computed labels** (`shape_label`, `source_label`, decision `label`). The Flask fallback would need them in the JSON. Add them only if the fallback is built.

## Redis connection

- **Fast failure when Redis is down.** redis-py retries with backoff by default, so a down Redis can stall each refresh for seconds. Planned for step 4: let `common.bus.connect()` accept connection options, and give the page a short connect timeout.

## Fake feed (`services/page/fake/`)

- **Ring version churn.** The feed pushes each fake ring once. A `--bump R-0001` option that re-sends a ring with a higher version and one more edge would test the "latest version wins" path on screen.

---

## Done

(nothing yet)
