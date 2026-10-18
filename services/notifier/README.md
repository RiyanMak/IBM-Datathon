# Reviewer bot (notifier)

**Reads:** Redis streams `cases` (consumer group `notifier`), `rings`, `decisions` · **Writes:** `decisions`, hash `notifier:messages`

The project's sentence is "the AI recommends and explains, a person decides." The page shows that on a screen; this service brings it to where investigators already work. For every new case it posts a card to a chat channel with three buttons: **Freeze accounts**, **Dismiss**, **Needs more info**. A click writes to the `decisions` stream, the same stream the page reads for its Frozen / Dismissed badges, so a decision made in chat shows on the dashboard with no extra work.

```
cases ──► notifier ──► card ──► adapter (Slack | console | Teams, post only)
             ▲                      │
   rings, decisions                 ▼ click
             └────────── decisions ◄┘
```

The core decides *what* to send and *what a click means*; an adapter only knows *how* to talk to one chat product. No business logic lives in an adapter.

| File | What it does |
|---|---|
| `notifier.py` | The loop: read `cases`, decide, hand the card to the adapter, handle clicks |
| `cards.py` | A `cases` message → a plain "card" dict (no Slack code) |
| `decisions.py` | Write a decision (the page's five fields plus `decided_by`, `channel`); read latest per ring |
| `suppression.py` | Was this ring dismissed recently? Did it change enough to show again? |
| `packet.py` | The case packet (Markdown) attached to the card |
| `adapters/base.py` | The adapter interface |
| `adapters/console.py` | Cards as text, clicks typed on stdin; no network |
| `adapters/slack.py` | Slack, Bolt for Python in Socket Mode |
| `main.py` | What docker compose runs: `python -m services.notifier.main` |

## The card

One chat message per ring, edited in place. A newer version of the ring, or the Granite summary replacing the template, updates the same message and shows "updated to v3" or "Granite summary arrived".

- Ring ID, version, shape, number of accounts, total in USD
- Both model scores (XGBoost on the transfers, GNN on the graph); a dash when a score is missing, never 0
- Up to eight key facts: the key account as a possible victim, received and sent on, share passed on, hours until it began passing money on, collector, origin, transfers just under the reporting threshold, number of transfers
- The top signals, then the five summary parts in order (summary, what happened, why it is suspicious, possible victim, suggested next step)
- Footer: who wrote the summary (Granite, fact-checked, or the template) and the fact-check note. If Granite failed the check the note says so; a clean card hiding a failing model would be dishonest.
- The decision, once made, with the buttons disabled; a link to the dashboard (`DASHBOARD_URL`)

## Run it

Every command runs from the repo root.

```
python -m services.notifier.main --console          # cards as text; type: freeze R-0042 [note], dismiss R-0042 [note], more_info R-0042 [note], quit
python -m services.notifier.main                    # adapter from NOTIFIER_ADAPTER (default console)
python -m services.notifier.main --adapter slack    # Slack (needs the three SLACK_ keys below)
python -m services.notifier.main --packet R-0042    # print the latest case packet for a ring and exit
python -m services.notifier.main --once             # handle what is waiting, then exit (smoke test)
```

Try it without the rest of the pipeline: start Redis, run `python -m services.casewriter.main --push-fake services/casewriter/fake_rings.json` and the case writer, then the notifier with `--console`. Three cards appear; `freeze R-0001` writes a decision the page shows as Frozen.

In compose: `docker compose up -d notifier`, and `docker compose logs -f notifier` to watch. The console adapter cannot read typed commands under compose (no terminal attached); decide on the page or use Slack.

If a chat adapter cannot start (library missing, token missing) the service says so and falls back to the console adapter, so a missing network never blocks the demo.

## Settings (`.env` at the repo root, git-ignored)

| Key | Meaning | Default |
|---|---|---|
| `NOTIFIER_ADAPTER` | `console`, `slack` or `teams` | `console` |
| `SLACK_BOT_TOKEN` | bot token, `xoxb-…` | |
| `SLACK_APP_TOKEN` | app-level token for Socket Mode, `xapp-…` | |
| `SLACK_CHANNEL` | channel ID, e.g. `C0123ABCD` | |
| `TEAMS_WEBHOOK_URL` | Teams Workflows webhook (notification only, not yet implemented) | |
| `DASHBOARD_URL` | link on every card | `http://localhost:8501` |
| `SUPPRESS_HOURS` | how long a dismissal keeps a ring quiet | `24` |
| `NOTIFIER_USER` | name recorded for console clicks | your login name |

Never paste tokens into chat, commits or screenshots. If one is exposed, revoke it in the Slack app settings and make a new one.

## What each button writes

One message on `decisions`, all values text. The first five fields are written by the page's own `services/page/decisions.build`, so the formats are identical:

| Field | Value |
|---|---|
| `ring_id` | e.g. `R-0042` |
| `version` | the ring version on the card the person saw |
| `decision` | **Freeze accounts** → `freeze`, **Dismiss** → `dismiss`, **Needs more info** → `more_info` |
| `note` | optional, at most 1000 characters |
| `decided_at` | UTC with milliseconds, e.g. `2026-10-18T14:02:11.123+00:00` |
| `decided_by` | extra: `slack:U0123ABC:jane.doe` (channel, user ID, display name) or `console:aryan` |
| `channel` | extra: `slack`, `teams`, `console` or `page` |

Readers that know only the page's fields ignore the two extras. The card then shows "Frozen by jane.doe at 2026-10-18 14:02 UTC" and its buttons are disabled; a second click on the same ring is ignored and logged, so the audit trail never holds two answers to one card. Anyone in the channel may click and their display name is recorded; an organisation would restrict the channel.

## Suppression (alert fatigue)

- When the latest decision on a ring is **Dismiss**, new versions of that ring are not posted again for `SUPPRESS_HOURS` (24 by default). They are still acknowledged and logged ("suppressed R-0042 v5: dismissed by jane at 2026-10-18 14:02 UTC"), and the existing card gets one quiet "dismissed" update.
- A big change lifts it: the ring grew by 2 or more accounts, or its total grew by 50% or more, since the dismissed card. The card is posted again with "re-opened: ring grew from 5 to 8 accounts".
- After the window the ring is posted again as a fresh card with its buttons enabled.
- A different ring that shares at least half its accounts with a recently dismissed one is posted as normal, with a note "related ring R-0042 dismissed by jane at …".
- **Freeze** and **Needs more info** never suppress.
- The `decisions` stream is the source of truth, so a restart keeps every suppression and the page sees the same decisions.
- Clock: both times are wall-clock. `decided_at` is UTC; the case writer's `written_at` has no zone and is read as UTC, which is exact in Docker and on the VM (local time is UTC there). When the case writer sends data time (`ring_last_seen`) the rule will switch to it.

## Reliability

- Pending messages are drained first after a restart, then new ones. `notifier:messages` (ring → message reference) means a restart never re-posts a card.
- Within one batch only the newest case per ring is shown. A slow Granite reply for v1 arriving after the template for v2 is skipped, so a card never goes backwards.
- A malformed case is logged once and acknowledged. A case the chat product refused (network down, rate limit) is left pending and retried a few seconds later. Redis being down is logged and retried every second.

## Teams

Teams is notification-only and **not yet implemented**. The plan is a post-only adapter (`adapters/teams_webhook.py`) sending an Adaptive Card to a Workflows webhook, with "decide in the dashboard" instead of buttons. A full Teams bot with buttons needs an Azure Bot registration, a public endpoint and tenant admin approval, and is future work; the adapter interface means that work replaces one file. `--adapter teams` currently falls back to the console with a message.

## Slack app setup (about 20 minutes, no code)

1. Create a free Slack workspace (or use one the team controls).
2. At api.slack.com/apps: **Create New App → From scratch**, name "Mule Ring Hunter", pick the workspace.
3. **Socket Mode → Enable.** Create an app-level token with scope `connections:write`. This is `SLACK_APP_TOKEN` (`xapp-…`).
4. **OAuth & Permissions → Bot Token Scopes:** add `chat:write`, `chat:write.public`, `files:write`. Install the app to the workspace. This gives `SLACK_BOT_TOKEN` (`xoxb-…`).
5. **Interactivity & Shortcuts → Enable** (with Socket Mode no request URL is needed).
6. Create a channel such as `#mule-ring-alerts`, invite the bot (`/invite @Mule Ring Hunter`), note the channel ID (channel details, bottom of the pane). This is `SLACK_CHANNEL`.
7. Put the three values in `.env` (root, git-ignored). Never paste tokens into chat, commits or screenshots. If a token is ever exposed, revoke it in the app settings and make a new one.

Socket Mode is an outbound websocket: no public URL and no inbound port are opened.

## Tests

```
python -m pytest -q services/notifier
```

fakeredis and the console adapter; no network, Slack or Granite. Cases are made by the case writer's own `write_case()` from `services/casewriter/fake_rings.json`.
