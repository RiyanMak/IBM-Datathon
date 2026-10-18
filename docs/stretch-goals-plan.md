# Stretch goals: evaluation, reviewer bot, case packet, runbook, suppression

Status as of 2026-10-17 (event day, about hour 8). Owner: Aryan, with whoever is free once the model work lands. It is the plan we agreed to start on **after** the scorer, ring service and GNN are in the repo, so that the people building those are not interrupted.

Update 2026-10-18: feature 1 is `scripts/evaluate_rings.py` (`--markdown` writes `evaluation.md` with the denominators, the attempts that started earlier, the fastest and slowest alerts and the limitations; D1 to D3 were not needed, it reads the patterns file directly); features 2, 3 and 5 are `services/notifier/` (console and Slack adapters; Teams not built); feature 4 is `docs/demo-runbook.md` and `scripts/stress_replayer.py`. The sections below are kept as the design record.

This document covers:

1. Why these five, and in which order
2. Where the project stands and what we are waiting for
3. Feature 1: evaluation harness (the pitch numbers)
4. Feature 2: reviewer bot for Slack, with Teams planned
5. Feature 3: case packet (one-page report per ring)
6. Feature 4: resilience and scale runbook
7. Feature 5: alert suppression
8. Cross-cutting changes (compose, requirements, env, README, security)
9. Contracts with neighbouring steps, in one place
10. Build order, time budget and cut line
11. Open decisions
12. Glossary

The team guide (`Mule Ring Hunter: Team Helper Guide`, `docs/teamguide.pdf`) is the source of truth for stream names and fields. Where this document and the guide disagree, the guide wins and this document should be fixed. Kevin's blueprint (`Mule_Ring_Hunter_Cleaning_Blueprint.md`) is the source of truth for the dataset counts used in section 3.

---

## 1. Why these five, and in which order

The judging playbook scores four categories, each out of 10: Technical Design, Novelty and Originality, Pitch and Delivery, Community Value. It also recommends two or more AI frameworks, containers, visible LinuxONE evidence, and predictive models paired with Granite. The architecture already satisfies all of that on paper. What is missing is **proof**: measured numbers, a human really deciding, and screenshots of it running on the platform.

| # | Feature | Category it moves | Depends on | Rough hours |
|---|---|---|---|---|
| 1 | Evaluation harness | Pitch (the numbers), Technical Design | `rings` stream from the ring service; patterns file | 2.5 |
| 2 | Reviewer bot (Slack now, Teams later) | Community Value, Novelty | `cases` stream (exists); `decisions` contract (exists) | 2 |
| 3 | Case packet | Novelty, Community Value | the `facts` field the case writer already sends | 0.75 |
| 4 | Resilience and scale runbook | Platform (highest weighted), Pitch | the VM, the scorer | 0.5 to write; Pair 3 executes |
| 5 | Alert suppression | Community Value | `decisions` stream (exists) | 0.3 |

Order of building: **1, then 2, then 3 and 5 as bolt-ons to 2, then 4.** Feature 1 first because the pitch numbers only exist if the harness is ready before the first full replay; without it we would be describing an architecture instead of a result. Feature 2 second because it is the demo beat and most of its inputs already exist.

Cut line if time runs out: ship 1 and 2. Features 3 and 5 are small additions to 2. Feature 4 is a document.

---

## 2. Where the project stands and what we are waiting for

### In the repo (`main` or a pushed branch) as of hour 8

| Piece | Where | State |
|---|---|---|
| Data cleaning | `common/clean_transactions.py`, `splits.py`, `fx.py`, `quality.py`, `scripts/clean_data.py`, `scripts/validate_clean.py` | Done, verified against Kevin's blueprint (all 35 reference checks pass). Outputs in `data/processed/v1/` on Aryan's laptop |
| Replayer (step 1) | `services/replayer/`, `common/streams.py` | Done, verified against a real Redis 7 (full test split, cap at ~200,000, resume works). PR open |
| Clue class (step 2) | `common/features.py` | On `main`. One `ClueBuilder`, 31 clues, compute-then-remember. Its live path reads exactly the fields the replayer sends |
| XGBoost training | `notebooks/train_xgboost.ipynb` | On `main`; model not yet saved to `models/` |
| Case writer (step 5) | `services/casewriter/` | Done offline; Granite not yet called live; Phase B (live-replay behaviour) and C (`case:<ring_id>` hash) of its plan still open |
| Investigator page (step 6) | branch `investigator-page`: `services/page/{models,state,decisions}.py`, `fake/` | Data layer done; `app.py` not yet written |
| Scorer service (step 3) | `services/scorer/` | Placeholder only |
| Ring service (step 4), GNN | `services/rings/` | Placeholder only |
| Platform | `Dockerfile` | Base image still `FROM <linuxone-jupyter-image>:<tag>`; `screenshots/` empty |

### What "the model is done" means for this plan

We start when **all** of these are on `main`:

- [ ] `services/scorer/main.py` reads `txns` with a consumer group, runs `ClueBuilder`, scores with `models/scorer.json`, writes `scored`.
- [ ] `models/feature_list.json`, `models/scorer.json`, `models/thresholds.json` exist (or a documented reason they live only on the VM).
- [ ] `services/rings/main.py` writes `rings` messages with the guide's fields, including `edges` with real `txn_id`s and `first_seen`.
- [ ] Someone has confirmed the scorer does **warm-up**: it loops `clean.csv` from the first row up to `test_start` through the same `ClueBuilder`, with no labels, before reading the stream. Without this every account looks brand-new at the start of the test period and the "new payee" and "account age" clues are wrong. The replayer deliberately does not send that history.

Until then: Aryan can do the data half of feature 1 (section 3.3, steps D1 to D3), which needs only the raw files, and set up the Slack app (section 4.4), which needs no code.

### How to re-scan when the time comes

From the repo root, read-only:

```
git fetch origin
git branch -r
git log --oneline --all --since="1 day ago"
git ls-tree -r --name-only origin/main services/ common/ models/
```

Then read `services/scorer/main.py` and `services/rings/main.py` and update section 9 (contracts) if anything differs from the guide.

---

## 3. Feature 1: evaluation harness

### 3.1 What it is, in plain words

The pipeline *produces* rings; nothing *grades* them. The patterns file `HI-Small_Patterns.txt` is the answer key: 370 known laundering attempts, each with a shape name and the exact transfers that make it up. The harness matches those transfers to our cleaned data, then compares the rings our system found against the attempts, and prints the numbers the pitch needs.

The numbers:

| Number | Definition | Why a judge cares |
|---|---|---|
| **Attempts caught** | An attempt is caught when some ring has at least half of its accounts inside that attempt's accounts (guide, "Numbers the pitch needs"). Report caught ÷ the number of attempts that lie **wholly in the test period** | Recall on real laundering operations, not on single rows |
| **Shape match rate** | Among caught attempts, how often our ring's shape equals the pattern file's shape (after normalising case and separators) | Proves the explanation is right, not just the alarm |
| **Time to alert** | Minutes of **data time** from the attempt's first transfer to the ring's `first_seen` (first version on the `rings` stream). Report the median and the 90th percentile | "We caught it 40 data-minutes in" |
| **Money still stoppable** | For each caught attempt, the share of its total USD that moved **after** our alert time. Report the median and the total across attempts | The impact number: dollars a bank could still have frozen |
| **Ring precision (secondary)** | Share of rings whose accounts are at least half inside some attempt. Report it, but say clearly that the patterns file is an incomplete answer key (it covers 3,209 of 5,177 laundering rows), so a ring that matches no attempt is **not** proven to be a false positive | Honesty slide |

### 3.2 Denominators (get this right or the number is wrong)

Kevin's blueprint, measured on our exact file (same SHA-256):

- 370 attempts, 3,209 pattern rows, all with label 1.
- Pattern rows per split: 1,177 train / 693 validation / 1,339 test.
- Attempts wholly inside one split: 104 train / 16 validation / **95 test**.
- Attempts that span a split boundary: 155.

Rules:

- The headline recall uses **95 wholly-test attempts** as the denominator. Spanning attempts are reported in a separate line ("of the 155 attempts that started before the test period, we caught N"). Never report caught ÷ 370.
- The replayer starts at `test_start` by default, so attempts that began earlier are only partly visible to the live system. That is why they are reported separately.
- If the demo replay starts later than `test_start` (`--start` some hours before a chosen attempt), the harness must take the actual replay start as an argument and count only attempts whose first transfer is at or after it.
- No tuning on these numbers. Thresholds are chosen on validation. The harness is run on the test period once for the pitch, and again only if something was broken.

### 3.3 The data half (Aryan, can start now): D1, D2, D3

These follow the laptop cleaning plan (`docs/Mule_Ring_Hunter_Laptop_Cleaning_Plan.md`, stages E and F) and reuse `common/data_contract.py`.

**D1. Accounts reference → `accounts_clean.csv`.**
Input `HI-Small_accounts.csv` (Kevin's SHA-256 `786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014`). Rename `Bank Name → bank_name`, `Bank ID → bank_id`, `Account Number → account_raw`, `Entity ID → entity_id`, `Entity Name → entity_name`; add `account_id` with `make_account_id`. All identifiers stay text. Checks: 518,581 rows, `account_id` unique, every `from_acct`/`to_acct` in `clean.csv` present (expected missing: 0), 3,493 reference accounts unused by transactions, 515,088 active accounts. This also settles the open question from the cleaning run: 58 banks appear with more than one zero-padding (`010` and `0010`); if the accounts table uses one canonical form per bank and every transaction endpoint matches, the merge was right.

**D2. Patterns → `patterns.csv`.**
Parse `BEGIN LAUNDERING ATTEMPT - <SHAPE>[: extra]` … `END LAUNDERING ATTEMPT` blocks. Reject nested or unclosed blocks and transfer lines outside a block. Assign zero-based `attempt_id`, `pattern_row_id`, `pattern_line`, `shape` (text before the colon, compound names such as `SCATTER-GATHER` kept). Match each pattern row to exactly one raw transaction on the full key: timestamp, both bank/account pairs (through the shared canonical helper), both native amounts as exact `Decimal` tokens, both currencies, payment format. Label is **not** in the key; verify it is 1 after matching. Scan the raw CSV once in chunks; keep every candidate `source_row` (do not let a dictionary overwrite a duplicate); require exactly one match per row; then map `source_row → txn_id, split` through `clean.csv`. Expected: 3,209 unique, 0 ambiguous, 0 missing. Save the columns from the laptop plan's contract (`attempt_id, shape, pattern_row_id, pattern_line, source_row, txn_id, ts, from_acct, to_acct, amt_paid, ccy_paid, amt_recv, ccy_recv, fmt, split`) plus a per-attempt table: `attempt_id, shape, n_rows, n_accounts, first_ts, last_ts, total_usd, split_membership` (`train`, `validation`, `test`, or `spans`).

**D3. `cleaning_manifest.json`.**
Input paths and SHA-256 of all three raw files, package versions, schema version, output paths and sizes, completion status (`core: done`, `accounts: done`, `patterns: done`), and the row counts above. One file a teammate can read to know they have the same release.

Proposed files: `common/accounts.py`, `common/patterns.py`, `scripts/clean_accounts.py`, `scripts/match_patterns.py`, `scripts/write_manifest.py`, tests under `tests/`. Outputs go to `data/processed/v1/` next to `clean.csv`. Same rules as the cleaning milestone: no silent drops, stop on contract violations, independent spot checks against the raw files, sample observations labelled as such.

### 3.4 The scoring half: `scripts/evaluate.py`

Inputs:

- `--rings`: either a live Redis (read `XRANGE rings - +`; the stream is small) or a JSON-lines dump of `rings` messages taken after a replay (`redis-cli XRANGE rings - +` piped through a tiny converter, or `r.xrange` from Python). The dump form is what we commit as evidence.
- `--patterns data/processed/v1/patterns.csv`
- `--clean data/processed/v1/clean.csv` (only `txn_id, ts, amount_usd, from_acct, to_acct`, for attempt money and timing)
- `--splits data/processed/v1/splits.json`
- `--replay-start` (ISO; default `test_start`)
- `--out data/processed/v1/evaluation.md` plus `evaluation.json`

Algorithm:

1. Build `attempt → set(accounts)`, `attempt → first_ts, last_ts, total_usd, shape, membership` from `patterns.csv`.
2. Keep attempts whose first transfer is at or after `--replay-start`; that is the headline set. Keep the rest as the "started earlier" set.
3. For each `ring_id`, take its **first** version's `first_seen` as the alert time and its **latest** version's `accounts` and `shape` for matching. (Decision recorded in section 11: alert time comes from the first version, because that is when a person could first have acted.)
4. Match: for each ring and each attempt, `overlap = |ring.accounts ∩ attempt.accounts|`. The ring catches the attempt if `overlap ≥ ceil(len(ring.accounts) / 2)`. If a ring catches several attempts, credit all of them but count the ring once for precision. If several rings catch one attempt, the attempt's alert time is the earliest.
5. Time to alert: `alert_ts − attempt.first_ts` in minutes. Negative values (alert before the attempt's first transfer, possible if a ring formed from other activity) are clamped to 0 and counted.
6. Money still stoppable: sum of `amount_usd` of the attempt's transfers with `ts > alert_ts`, divided by the attempt's `total_usd`.
7. Shape match: `normalise_shape(ring.shape) == normalise_shape(attempt.shape)`, using the same normalisation as `services/page/models.py` (`'SCATTER-GATHER'`, `'scatter_gather'`, `'Scatter gather'` all become `scatter-gather`). `random` attempts have no equivalent ring shape; report them as "not nameable" rather than as a miss.
8. Write `evaluation.md` with: the replay start, the denominators, each number with its definition, a per-shape breakdown (attempts of each shape, caught, shape matched), the five best and five worst time-to-alert cases with their `ring_id` and `attempt_id`, and a limitations section (incomplete answer key; equal-minute ordering is a convention; alert time is data time not wall time).

Tests (meaningful, on fixtures):

- **Perfect rings**: run `services/casewriter/patterns_to_rings.py` to turn 40 attempts into rings (it drops self-transfers and small attempts, so expect "caught" ≈ 100% of those it produced), feed them in with `first_seen` = the attempt's first transfer: recall ≈ 100%, shape match 100%, time to alert 0, money stoppable 100%.
- **Fake rings** (`services/casewriter/fake_rings.json`, invented accounts): 0 caught, 3 rings counted as unmatched in precision.
- **Half-overlap edge cases**: a 4-account ring with 2 accounts in an attempt counts; with 1 it does not. A 3-account ring needs 2.
- **Timing**: a ring whose `first_seen` is 40 minutes after an attempt's first transfer, with the attempt's money 30% before and 70% after that minute: time to alert 40, stoppable 70%.
- **Denominator**: with `--replay-start` after some attempts began, those move to the "started earlier" set.
- **Shape normalisation**: `SCATTER-GATHER` vs `scatter-gather` match; `random` is reported as not nameable.
- Round trip: `evaluation.json` loads and holds the same numbers as the Markdown.

Acceptance: the perfect-ring test gives the expected ceilings; the fake-ring test gives zero; the report states denominators and limitations in plain words; the script runs from a committed JSON-lines dump without Redis.

### 3.5 What the harness needs from teammates

- **Pair 2 (ring service):** every `rings` message carries `ring_id`, `version` (increments on every republish), `shape`, `accounts` (JSON list, hub accounts excluded), `edges` (JSON list with real `txn_id`s), `first_seen`, `last_seen`. This is the guide's contract; nothing new.
- **Pair 3:** after the final full replay on the VM, dump the stream once: `redis-cli XRANGE rings - +` to a file, and copy it to the repo under `data/evaluation/rings_dump.txt` (small; this one is committed as evidence). A short Python one-liner that writes JSON lines is in the runbook (section 6).
- **Whoever presents:** use the 95-attempt denominator; report spanning attempts separately; never describe the file as 10 days of uniform activity (it runs to Sept 18 and the tail is dense in positives).

---

## 4. Feature 2: reviewer bot (`services/notifier/`)

### 4.1 What it is, in plain words

A seventh service. It listens to the `cases` stream (written by the case writer) and, for each new case, posts a card to a chat channel: ring ID, shape, number of accounts, both model scores, the five-part summary, a few key facts, a label saying whether Granite (fact-checked) or the template wrote it, and three buttons: **Freeze accounts**, **Dismiss**, **Needs more info**. A click writes a message to the `decisions` stream, the same stream the investigator page reads for its Frozen / Dismissed badges, so a decision made in chat shows up on the dashboard with no extra work.

Why it matters: the project's sentence is "the AI recommends and explains, a person decides." The page shows that on a screen; the bot brings it to where investigators already work. It also makes the Community Value category concrete and gives the video a visible moment: phone buzzes, investigator taps Freeze, dashboard badge flips.

### 4.2 Slack first, Teams second: the reasoning

The audience is banking professionals, who use Slack or Microsoft Teams, not Discord. Both are planned; Slack is built first because it can be finished in an afternoon and Teams cannot.

| Capability | Slack | Microsoft Teams |
|---|---|---|
| Post a card | `chat.postMessage` with Block Kit blocks | Adaptive Card through a **Workflows** webhook (the old "Office 365 Connectors" are retired) |
| Buttons that write back to us | **Bolt for Python in Socket Mode**: our service opens an outbound websocket to Slack and receives clicks over it. No public URL, no inbound port. Works from the VM or a laptop | Requires an Azure Bot registration, a public HTTPS endpoint (or a dev tunnel), and in most organisations a tenant admin's approval. Not an afternoon job |
| Update a card in place | `chat.update` | Not supported for webhook posts |
| Attach a file (the case packet) | `files_upload_v2` | Not supported for webhook posts; a link is possible |
| Setup by us | Free workspace, create an app, two tokens: about 20 minutes | Needs a Teams tenant we control; university tenants are usually locked down |
| Cost | Free tier is enough | Free tier exists but the bot route needs Azure |

Design consequence: the notifier has a **channel-agnostic core** and thin **adapters**. The core decides *what* to send and *what a click means*; an adapter knows *how* to talk to one chat product. Slack gets the full adapter (post, update, buttons, file). Teams gets a **post-only webhook adapter** (Adaptive Card with the summary and a link to the dashboard; decisions are made on the page). A full Teams bot with buttons is listed as future work, not promised for the event. The pitch line: "the investigator gets the case where they already work: Slack today, Teams through the same adapter; decisions from either land on the same audit stream."

### 4.3 Module layout

```
services/notifier/
  __init__.py
  main.py          what docker compose runs: python -m services.notifier.main
  notifier.py      the loop: read cases (consumer group), build a card, hand it to the adapter; handle clicks
  cards.py         case message -> a neutral "card" dict (title, fields, summary parts, buttons); no Slack code
  decisions.py     write a decision to the decisions stream (same fields as the page); read latest per ring
  suppression.py   feature 5: was this ring dismissed recently? (reads decisions)
  packet.py        feature 3: facts + summary -> Markdown case packet
  adapters/
    __init__.py
    base.py        the Adapter interface: post(card) -> message_ref, update(message_ref, card), upload(message_ref, filename, text), on_click(callback)
    console.py     prints cards to stdout; "clicks" come from a tiny CLI; used for tests and for a demo without network
    slack.py       Bolt for Python, Socket Mode; Block Kit rendering; button handlers
    teams_webhook.py   post-only Adaptive Card via a Workflows webhook URL (stretch within the stretch)
  test_notifier.py tests with fakeredis and the console adapter; Slack rendering tested as pure functions (blocks JSON)
  README.md        how to set up the Slack app, how to run alone and in compose, what each button does
```

Rules, matching the page's plan: no business logic in the adapters; `cards.py`, `decisions.py`, `suppression.py`, `packet.py` import nothing from Slack; tests never need network.

### 4.4 Slack app setup (Aryan, no code needed; do this any time)

1. Create a free Slack workspace (or use one the team controls).
2. At api.slack.com/apps: **Create New App → From scratch**, name "Mule Ring Hunter", pick the workspace.
3. **Socket Mode → Enable.** Create an app-level token with scope `connections:write`. This is `SLACK_APP_TOKEN` (`xapp-…`).
4. **OAuth & Permissions → Bot Token Scopes:** add `chat:write`, `chat:write.public`, `files:write`. Install the app to the workspace. This gives `SLACK_BOT_TOKEN` (`xoxb-…`).
5. **Interactivity & Shortcuts → Enable** (with Socket Mode no request URL is needed).
6. Create a channel such as `#mule-ring-alerts`, invite the bot (`/invite @Mule Ring Hunter`), note the channel ID (channel details, bottom of the pane). This is `SLACK_CHANNEL`.
7. Put the three values in `.env` (root, git-ignored). Never paste tokens into chat, commits or screenshots. If a token is ever exposed, revoke it in the app settings and make a new one.

### 4.5 Behaviour of the service

- **Reading `cases`:** consumer group `notifier`, `XREADGROUP … count=10, block=5000`, `XACK` after handling, `BUSYGROUP` tolerated on restart, drain pending messages (`id="0"`) once at startup before switching to `>`. Same pattern as the case writer.
- **One Slack message per ring, updated in place:** keep `ring_id → message_ref` in memory and in a Redis hash `notifier:messages` so a restart does not re-post. When a newer case arrives for a ring already posted (a higher `version`, or the same version upgraded from `template` to `granite`), call `update` instead of `post`. The card shows "updated to v3" and "Granite summary arrived" when the source flips.
- **Coalescing:** within one batch keep only the newest case per `ring_id` (the case writer's plan B1 does the same on its side).
- **Buttons:** three `actions` buttons with `action_id` `decide_freeze`, `decide_dismiss`, `decide_more_info` and `value` = JSON `{ring_id, version}`. On click: write the decision (section 4.6), then update the card to show "Frozen by @name at 14:02 UTC" and disable the buttons. "Needs more info" opens a modal with a note field; the other two accept an optional note through the same modal if the user clicks a small "add note" button first (keep the one-tap path fast).
- **Who may click:** anyone in the channel; the decision records their Slack display name. For the demo that is enough. The README says an organisation would restrict the channel.
- **Card content** (from the `cases` message and its `facts` JSON): `ring_id`, `version`, shape label (reuse the label map idea from `services/page/models.py`; copy the small dict rather than import across services, or move it to `common/labels.py` if the page owner agrees), `n_accounts`, `xgb_score` / `gnn_score` (dash when empty), `total_usd`, `key_account` as a possible victim, `passed_on_pct`, `hours_to_pass_on`, `collector`, `top_reasons`, then the five summary parts in order, then a footer: source label, `check_note` in small text, `written_at`, a link to the dashboard (`DASHBOARD_URL`, default `http://localhost:8501`).
- **Fact-check visibility:** if `source` is `template` because Granite failed, the footer says so in one short line. Hiding a failing Granite behind a clean card would be dishonest and the judges may ask.
- **Robustness:** every `json.loads` and float conversion in `try/except`; a malformed case is logged once and acknowledged; Redis connection errors retry with a short sleep; Slack rate limits (HTTP 429) honoured with the SDK's retry handler; if Slack is unreachable the service logs and keeps the message pending (does not ack), so it is sent when the connection returns.
- **Console adapter:** `python -m services.notifier.main --console` prints cards as text and accepts `freeze R-0042`, `dismiss R-0042 note…` on stdin. This is the no-network demo path and what the tests use.

### 4.6 The decisions contract (exact)

Anirudh's `services/page/decisions.py` writes:

```
ring_id      text
version      text (the ring version the person saw; "" if unknown)   <- already an extra beyond the guide
decision     one of freeze | dismiss | more_info
note         text, at most 1000 characters
decided_at   UTC ISO with milliseconds, e.g. 2022-09-08T14:02:11.123+00:00
```

The notifier writes the same five fields, same formats, and adds two more:

```
decided_by   e.g. "slack:U0123ABC:jane.doe" (channel, user id, display name) or "console:aryan"
channel      "slack" | "teams" | "console" | "page"
```

Readers that know only the guide's four fields ignore the extras, exactly as they already must for `version`. Open item for Anirudh: whether the page should also write `channel: "page"` so the audit log is uniform; it costs one line and we will not ask for it until the page is stable.

Reading decisions (for badges on the card and for suppression): `XREVRANGE decisions + - COUNT 500`, latest per `ring_id`, same as the page's plan.

### 4.7 Tests

- `cards.build(case)` from a real case produced by `python -m services.casewriter.main --file services/casewriter/fake_rings.json`: all five parts present in order; source label correct for `granite` and `template`; missing `gnn_score` renders as a dash; `facts` absent still produces a card (fewer fields, no crash).
- Slack rendering as pure functions: `slack.blocks(card)` is valid Block Kit (section/fields/actions types; text under Slack's 3,000-character section limit, long summaries truncated with "…" and the full text in the packet).
- Decision write: fields and formats equal `services/page/decisions.build` for the shared five fields (a test imports both and compares); `decided_by` and `channel` present; invalid decision rejected.
- Loop with fakeredis and the console adapter: three cases on `cases` produce three posts; a higher version for one ring produces an update not a post; a template→granite flip at the same version produces an update with the "Granite summary arrived" marker; restart (new loop instance, same fakeredis) re-posts nothing because `notifier:messages` is restored.
- A click on `decide_freeze` writes one `decisions` message and the card shows the decision; a second click on the same ring is ignored with a notice.
- Suppression and packet tests are in sections 7 and 5.

Acceptance (from the guide's style of "done when"): a fake ring pushed with the case writer's `--push-fake` appears as a Slack card within a few seconds; tapping Freeze writes a `decisions` message that the page's `state.py` reads as Frozen; `docker compose restart notifier` posts no duplicates; the service runs with no Slack tokens at all in console mode, so a missing network never blocks the demo.

### 4.8 Teams: what is planned and what is not

Planned for the event, only if Slack is finished and verified:

- `adapters/teams_webhook.py`: `post(card)` sends an Adaptive Card (version 1.4) to `TEAMS_WEBHOOK_URL` with the same fields as the Slack card and an "Open in dashboard" link; `update` and `upload` are no-ops that log once; no buttons. A click is not possible on this route, so the card says "decide in the dashboard".
- One test that the Adaptive Card JSON validates against the fields we expect.
- The README states plainly that Teams is notification-only in this version.

Not for the event, written down so nobody is surprised: a full Teams bot (Bot Framework, Azure registration, public endpoint, admin consent) is a day of work and organisational approvals. The adapter interface is designed so that work replaces one file.

---

## 5. Feature 3: case packet (`services/notifier/packet.py`)

### 5.1 What it is

A one-page Markdown document per ring, built from the same facts pack the case writer computes and the summary it wrote. It is the draft of what an investigator would file; in the US that is a Suspicious Activity Report. Every number in it comes from the facts pack; every transfer line carries its `txn_id` so anyone can trace it back to `clean.csv` and from there (through `source_row`) to the raw CSV line.

Why it matters: a dashboard widget is what every team shows. A downloadable, traceable report shows we understand the investigator's actual job, and "every number traces to a `txn_id`" is a strong answer to "how do you know the AI did not make this up."

### 5.2 Contents, in order

1. Header: ring ID, version, shape label, generated at (UTC), source of the narrative (Granite fact-checked / template), `check_note`.
2. Summary: the five parts under their labels.
3. Key facts table: `total_usd`, `n_accounts`, `n_transfers`, `first_seen`, `last_seen`, `duration_hours`, `key_account` (as "possible victim"), `key_received_usd`, `key_sent_usd`, `passed_on_pct`, `hours_to_pass_on`, `origin`, `collector`, `n_just_under_threshold` (with `reporting_threshold_usd`), `xgb_score`, `gnn_score`, `top_reasons`.
4. Accounts involved: the `accounts` list, with roles (possible victim / origin / collector / other).
5. Transfers: a table of `edges`: `ts`, `txn_id`, `from`, `to`, `amount_usd`, `score`. Sorted by time. Capped at 200 rows with a note if longer.
6. Data lineage: "Transaction IDs refer to `clean.csv` release v1 (input SHA-256 b19d39f5…). Amounts in USD use exchange rates fitted on the training period only (`fx_rates.json`)." Pull the hash from `cleaning_manifest.json` when present.
7. Decision log: every `decisions` message for this ring (who, what, when, note).
8. Disclaimer: synthetic data; "activity consistent with", not an accusation; a person decides.

### 5.3 Where it is used

- The Slack adapter uploads it as `case_<ring_id>_v<version>.md` attached to the card (`files_upload_v2`), and re-uploads on update.
- The page can offer "Download case packet" by calling `packet.render(case, decisions)`; this is Anirudh's call, no change needed on his side until he wants it.
- `python -m services.notifier.main --packet R-0042` writes the file to stdout for a screenshot.

### 5.4 Tests

- Rendered from each of the three fake cases: contains all five parts, every `txn_id` from the edges, no number that is not in the facts pack (reuse the idea of the case writer's `numbers_in` check, adapted: the packet may contain the `txn_id`s and timestamps as well).
- Edges capped at 200 with the note; empty `edges` produces a packet with a "no transfer detail" line rather than a crash.
- Decision log section lists two fake decisions in time order.

---

## 6. Feature 4: resilience and scale runbook (`docs/demo-runbook.md`)

### 6.1 What it is

A checklist of the exact commands to run on the LinuxONE VM, the expected output of each, and the screenshot to take. Platform is the heaviest-weighted category and it is scored on evidence. The runbook makes sure the evidence is collected deliberately, not improvised at 2 AM, and that each screenshot maps to a rubric line.

This is a document plus one small script; the execution belongs to Pair 3 because it needs the VM and the scorer.

### 6.2 Contents

For each item: command, expected output, screenshot filename under `screenshots/`, which judging point it serves.

| Item | Command (on the VM) | Expected | Judging point |
|---|---|---|---|
| Platform identity | `uname -m; hostname; grep -c nnpa /proc/cpuinfo` | `s390x`, the VM name, `nnpa` count 0 or more | Runs on LinuxONE; Telum present or not |
| Containers up | `docker compose ps` | all services `Up`; `redis` shows **no** published port | Containerization; Security |
| First message flows | start replayer with `--limit 100`; `docker compose logs --tail 20 scorer` | scored lines appear | Technical design |
| Latency | the page's p50/p99, or `redis-cli XREVRANGE scored + - COUNT 1` showing `sent_at` and `scored_at` | p99 under 50 ms per the guide's target | Compute |
| Scale out | `docker compose up -d --scale scorer=3`; replay 60 data-minutes at `--speed 0`; compare wall time with 1 vs 3 scorers | higher throughput; `redis-cli XINFO GROUPS txns` shows 3 consumers sharing | Scalability |
| Kill and recover | during a replay: `docker compose kill scorer` (one replica), wait 10 s, `docker compose up -d scorer`; `redis-cli XPENDING txns scorers` | pending count goes back to 0; no gap in `scored` `txn_id`s | Resilience |
| Replayer resume | `Ctrl+C` the replayer; run again with `--resume` | continues at the next `txn_id`, no duplicates (verify with `XREVRANGE` before and after) | Resilience |
| Stream cap | after a `--speed 0` full replay: `redis-cli XLEN txns` | about 200,000, not 1,015,882 | Memory safety |
| Telum | run the Snap ML vs XGBoost timing script from step 3 on 10,000 test rows | a two-row table; or the honest "nnpa absent, CPU latency shown" line | Compute |
| Decisions audit | `redis-cli XRANGE decisions - +` after clicking in Slack and on the page | both decisions, with `channel` and `decided_by` | Transparency; Community value |
| Security | `cat .gitignore`; `docker compose ps` (no Redis port); `docker compose exec scorer id` showing the non-root user | | Security |
| Evaluation evidence | `redis-cli XRANGE rings - +` dumped to a file; `python -m scripts.evaluate …` | `evaluation.md` written | Pitch numbers |

The runbook also fixes the **order**: identity and containers first (needs only redis + replayer + casewriter, which exist today), then the model-dependent items.

### 6.3 Script: `scripts/stress_replayer.py`

Optional. Runs the replayer at `--speed 0` into a stream with a short `--maxlen` and reports messages per second, so Pair 3 has a reproducible throughput number without hand-timing. On Aryan's laptop against Docker Redis 7 the replayer already measured about 29,000 messages per second over the full test split; the number on the VM is the one to quote.

### 6.4 What Pair 3 should know now

- The `Dockerfile` base image placeholder must be filled before anything else; it is the first line of the runbook.
- The scorer must use `XREADGROUP` with a consumer group and `XACK` after scoring. Without a group, `--scale scorer=3` makes every replica score every message, and the kill-and-recover demo cannot work.
- The data files go in `~/shared/data/` (`clean.csv`, `splits.json`, `fx_rates.json`, `HI-Small_Patterns.txt`, and later `patterns.csv`) and the models in `~/shared/models/`, as in the team guide. Compose mounts `~/shared` as `/data`, so the services default to `/data/data/<name>` and `/data/models/<name>` (`common/paths.py`). `DATA_DIR`, `MODELS_DIR`, `CLEAN_PATH` and `SPLITS_PATH` override them.

---

## 7. Feature 5: alert suppression (`services/notifier/suppression.py`)

### 7.1 What it is

When an investigator dismisses a ring, the notifier remembers that and does not post that ring again (new versions included) for a configurable window, default 24 data-hours or until the ring changes materially. The card for any later ring that shares most accounts with a dismissed one shows "related ring R-0042 dismissed by @name at …". Alert fatigue is the first complaint about real anti-money-laundering systems; this turns "a person decides" into "and the system listens."

Lives entirely inside the notifier; nobody else changes anything.

### 7.2 Rules

- Source of truth is the `decisions` stream (never an in-memory flag alone), so a restart keeps the suppressions and the page sees the same decisions.
- A ring is suppressed if its latest decision is `dismiss` and `decided_at` is within `SUPPRESS_HOURS` (env, default 24) of the new case's `written_at`. Both are wall-clock UTC in the current contracts; if the case writer adds `ring_last_seen` (its plan B6), switch to data time and say so in the README.
- A suppressed ring is still **acknowledged** on `cases` and still **logged** ("suppressed R-0042 v5: dismissed by jane at 14:02"), and its card is updated once with a small "dismissed" footer; it just does not re-notify (no new message, no Slack notification).
- "Material change" that lifts suppression: `n_accounts` grows by at least 2, or `total_usd` grows by at least 50% compared with the version that was dismissed. Then the card is re-posted with "re-opened: ring grew from 5 to 8 accounts".
- Related-ring note: if a new ring shares at least half its accounts with a dismissed ring, the card carries the note but is **not** suppressed (a different ring deserves a look).
- `freeze` and `more_info` never suppress.

### 7.3 Tests

- Dismiss R-1 at t; a v2 of R-1 at t+1h is suppressed (no post, acked, logged); at t+25h it is posted.
- v2 that grows from 5 to 8 accounts within the window is re-posted with the re-opened marker.
- A new ring R-2 sharing 3 of its 4 accounts with dismissed R-1 is posted with the related-ring note.
- Freeze never suppresses.
- Restart: suppression survives because it is read back from `decisions`.

Optional, only if Pair 2 has time: the same lookup as a small function they can call in the ring service so dismissed rings are not even re-published. Not required for the feature to count.

---

## 8. Cross-cutting changes

- **`docker-compose.yml`:** add a `notifier` service: `build: .`, `command: python -m services.notifier.main`, `env_file: .env`, `depends_on: redis`, volume `~/shared:/data` (for the manifest hash in packets). No ports.
- **`requirements.txt`:** add `slack-bolt` and `slack-sdk` (pure Python; no s390x build risk). `fakeredis` stays in `requirements-dev.txt`. Add both libraries to the README's Libraries section with links the moment they are imported (competition rule).
- **`.env.example`:** add, with comments: `SLACK_BOT_TOKEN=`, `SLACK_APP_TOKEN=`, `SLACK_CHANNEL=`, `TEAMS_WEBHOOK_URL=` (optional, notification-only), `DASHBOARD_URL=http://localhost:8501`, `SUPPRESS_HOURS=24`, `NOTIFIER_ADAPTER=slack` (or `console`, `teams`).
- **Security slide additions:** tokens only in `.env`; Socket Mode means no inbound port is opened for Slack; the notifier runs as the image's non-root user; decisions are an append-only audit stream with who/when/channel.
- **README:** a "Reviewer bot" section mirroring the case writer's: what it does, setup, run alone (`--console`), run in compose, what each button writes.
- **Models must not read any of this.** `decisions`, `cases`, suppression state and pattern membership are never features. Pair 2's feature allowlist is `common/features.py`'s `FEATURE_NAMES`; nothing here touches it.

---

## 9. Contracts with neighbouring steps, in one place

All values on Redis are text; lists and objects are JSON text.

| Stream / key | Writer | Readers | Fields | Status |
|---|---|---|---|---|
| `txns` | replayer | scorer | `txn_id, ts, from_acct, to_acct, amount_usd, amt_paid, ccy_paid, amt_recv, ccy_recv, fmt, sent_at` (`common/streams.py`) | implemented |
| `scored` | scorer | rings, page | everything in `txns` plus `score, flagged, reasons, clues, scored_at` | pending (scorer) |
| `rings` | ring service | casewriter, page, **evaluate.py** | `ring_id, version, shape, n_accounts, accounts, edges[{txn_id,from,to,amount_usd,ts,score}], xgb_score, gnn_score, reasons, first_seen, last_seen` | pending (rings) |
| `cases` | casewriter | page, **notifier** | `ring_id, version, summary{5 parts}, source, check_note, written_at, facts` (+ planned `ring_last_seen, written_after_s`) | implemented |
| `decisions` | page, **notifier** | page, notifier, audit | `ring_id, version, decision, note, decided_at` + notifier adds `decided_by, channel` | page side implemented |
| `notifier:messages` (hash) | notifier | notifier | `ring_id → adapter message reference` | new |
| `stats:casewriter` (hash) | casewriter (planned C2) | page, notifier footer | `cases_written, granite_tried, granite_passed` | planned by casewriter |

Files in `~/shared/data` (`/data/data` inside containers): `clean.csv`, `splits.json`, `fx_rates.json`, `HI-Small_Patterns.txt`, and after D1 to D3: `accounts_clean.csv`, `patterns.csv`, `cleaning_manifest.json`. Models are in `~/shared/models` (`/data/models`).

---

## 10. Build order, time budget and cut line

Assumes two people once the model work lands; one person roughly doubles the wall time.

| Order | Task | Who | Hours | Done when |
|---|---|---|---|---|
| 0 (now) | D1 accounts, D2 patterns, D3 manifest | Aryan | 1.5 | counts match Kevin's blueprint; `patterns.csv` written; manifest written |
| 0 (now) | Slack app and tokens in `.env` | Aryan | 0.3 | a test `chat.postMessage` from a Python one-liner lands in the channel |
| 1 | `scripts/evaluate.py` + tests | A | 1.0 | perfect-ring and fake-ring tests pass; report readable |
| 2 | notifier core + console adapter + decisions + tests | B | 1.0 | three fake cases print; a console click writes `decisions` |
| 3 | Slack adapter + update-in-place + buttons | B | 1.0 | `--push-fake` rings appear in Slack; Freeze shows on the page's badge |
| 4 | packet.py + Slack upload | A | 0.75 | packet attached; packet test passes |
| 5 | suppression.py | A | 0.3 | five suppression tests pass |
| 6 | compose, requirements, env, README | B | 0.3 | `docker compose up -d notifier` stays up in console mode |
| 7 | demo-runbook.md + stress script | A | 0.5 | Pair 3 has walked through it once |
| 8 | Teams webhook adapter | whoever is free | 0.5 | a card lands in a Teams channel, or the item is dropped |
| Final | full replay on the VM → rings dump → `evaluate.py` → numbers in the pitch | Pair 3 + Aryan | 0.5 | `evaluation.md` committed under `data/evaluation/` |

Cut line: 0 to 3 are the must-haves. 4 and 5 are small. 6 is required for 3 to count on the platform. 7 is a document. 8 is dropped first.

Hard stop: the guide freezes code at hour 16 (midnight Chicago). Nothing new starts after hour 14.

---

## 11. Open decisions

| # | Decision | Recommendation | Who decides |
|---|---|---|---|
| 1 | Extra `decisions` fields `decided_by`, `channel` | Yes; follows the page's own precedent of adding `version` | Anirudh |
| 2 | Alert time for time-to-alert: first ring version or the version that first reached half overlap | First version's `first_seen` (when a person could first act); report the other as a secondary line if time allows | Aryan / Pair 1 |
| 3 | Where `packet.py` lives | `services/notifier/` (stays out of the casewriter folder); move to `common/` if the page also uses it | Aryan, Ayan |
| 4 | Shape label map shared between page and notifier | Copy the small dict for now; propose `common/labels.py` after the event freeze is not a risk | Anirudh |
| 5 | Suppression clock: wall time (`written_at`) or data time (`ring_last_seen`, not yet sent) | Wall time now; switch when the casewriter sends data time | Aryan |
| 6 | Teams at all during the event | Only after Slack is verified end to end; notification-only | Aryan |
| 7 | Who may click in Slack | Anyone in the private channel; record display name | team |
| 8 | Where the evaluation evidence is committed | `data/evaluation/` (small files, un-ignore that folder explicitly in `.gitignore`) | Aryan |
| 9 | Branch | New `stretch-goals` branch off `main` after the replayer PR merges; one PR per feature (evaluation, notifier) so reviews stay small | Aryan |

---

## 12. Glossary

| Term | Plain meaning |
|---|---|
| Answer key / patterns file | The dataset's list of 370 known laundering attempts with their transfers and shape names. Partial: it covers 3,209 of the 5,177 laundering rows |
| Recall | Of the real laundering attempts, the share we caught |
| Precision | Of the rings we raised, the share that were real laundering. Here only a lower bound, because the answer key is incomplete |
| Time to alert | Data-time minutes from an attempt's first transfer to our first ring containing it |
| Money still stoppable | The share of an attempt's money that moved after our alert, so a bank could still have frozen it |
| Consumer group | A Redis feature that splits a stream's messages among several readers and remembers which were handled (`XACK`) |
| Socket Mode | Slack's way of delivering events over a connection our app opens outbound, so we need no public web address |
| Block Kit | Slack's JSON format for message layout (sections, fields, buttons) |
| Adaptive Card | Microsoft's JSON format for cards in Teams |
| Workflows webhook | Teams' current way to accept a posted card from an outside program (replaces Office 365 Connectors) |
| Adapter | A thin module that knows how to talk to one chat product; the rest of the notifier does not care which one is in use |
| Suppression | Not re-alerting on something a person already dismissed, for a set time |
| SAR | Suspicious Activity Report, the document a US bank files with regulators; our case packet is a draft in that spirit |
| Warm-up | Feeding an account's history from before the demo start into the clue class so its clues are right from the first live message |
| Data time vs wall time | Data time is the timestamp inside the dataset (September 2022). Wall time is the real clock during the demo |
