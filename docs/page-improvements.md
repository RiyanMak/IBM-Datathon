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
- **The Granite pass rate counts case messages, not rings.** It switches to the case writer's own numbers once `stats:casewriter` exists (case writer plan, C2); the case writer now keeps it, so this only applies to an older case writer.
- **`PageState.to_dict()` leaves out the computed labels** (`shape_label`, `source_label`, decision `label`). The Flask fallback would need them in the JSON. Add them only if the fallback is built.

## Page layout (`services/page/app.py`)

- **Long facts table.** A ring with every fact present gives about a dozen rows beside the reasons. If it feels long during the demo, show the first five and put the rest in an expander.
- **Pin demo rings.** Once Pair 1 picks the two or three demo attempts, a "pinned" option at the top of the ring list would keep them in view during a busy replay.

---

## Done

- **Fast failure when Redis is down** (2026-10-17, step 4). `common.bus.connect()` accepts redis options; the page uses a 1 s connect timeout and no retries, and shows a banner.
- **Dollar amounts garbled as maths** (2026-10-17, step 4). Found on screen: Streamlit read `$...$` as a formula. All data shown through Markdown is escaped by `fmt.md()`.
- **`case:<ring_id>` hash** (2026-10-18). `state.latest_case()` reads the hash first and scans the stream only when it is missing.
- **Case times in UTC** (2026-10-18). The case writer writes `written_at` with a zone; `fmt.wall_time()` already shows zoned times as UTC.
- **Ring version churn** (2026-10-18). `python -m services.page.fake.feed --bump R-0001` re-sends a fake ring with version + 1 and one more transfer.
- **Decisions say where they came from** (2026-10-18). `decisions` messages carry `channel` ("page") and `decided_by` (empty on the page), the same fields the Slack notifier writes.
