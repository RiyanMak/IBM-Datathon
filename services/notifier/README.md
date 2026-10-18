# Reviewer bot (notifier)

**Reads:** Redis streams `cases` (from the case writer), `rings` and `decisions` · **Writes:** `decisions` and the hash `notifier:messages`

The AI recommends and explains, a person decides. For each new case the notifier shows a card: ring id, shape, accounts, both model scores, total moved, the possible victim, the five-part summary, and whether Granite (fact-checked) or the template wrote it. The investigator answers **freeze**, **dismiss** or **more_info**, and the answer goes to the same `decisions` stream the page reads, so the dashboard badge changes with no extra work.

This version has one adapter, the console: cards are printed as text and decisions are typed on stdin. Chat adapters (Slack, Teams) are future work; each would be one file in `adapters/` behind the interface in `adapters/base.py`.

| File | What it does |
|---|---|
| `notifier.py` | The loop: consumer group `notifier` on `cases`, one card per ring, updates, clicks |
| `cards.py` | Case message → neutral card (no chat code) |
| `decisions.py` | Writes and reads `decisions` |
| `suppression.py` | Does not re-post a ring dismissed recently |
| `packet.py` | Case packet: one Markdown page per ring |
| `adapters/` | `base.py` (interface), `console.py` |
| `main.py` | What docker compose runs: `python -m services.notifier.main` |

## Run it

```bash
.venv/bin/python -m pytest -q services/notifier            # no Redis or network needed

# on a laptop, against a local Redis
REDIS_HOST=localhost python -m services.casewriter.main --push-fake services/casewriter/fake_rings.json
REDIS_HOST=localhost python -m services.casewriter.main --no-granite      # Ctrl-C once the cases are written
REDIS_HOST=localhost python -m services.notifier.main --console           # then type: freeze R-0001 bank confirmed
REDIS_HOST=localhost python -m services.notifier.main --packet R-0001 > case_R-0001.md
```

`--once` handles what is waiting and exits. In compose it runs as the `notifier` service (`docker compose logs -f notifier` shows the cards). With no terminal attached it posts cards and takes no typed decisions; decide on the page.

## What a decision writes

`freeze R-0042 note`, `dismiss R-0042 note`, `more_info R-0042 note` (the note is optional, at most 1,000 characters) each append one message to `decisions`:

```
ring_id, version, decision, note, decided_at     exactly as the page writes them
decided_by   "console:<user>"
channel      "console"
```

A second decision on a ring already decided for that version is refused with a notice; a newer version of the ring brings the choices back.

## Behaviour

- One card per ring. `notifier:messages` maps ring id → message record, so a restart re-posts nothing. A higher version, or a Granite summary replacing the template at the same version, updates the card ("updated to v3", "Granite summary arrived"). Within one read, only the newest case per ring is shown.
- Pending messages are drained at start. A malformed case is logged and acknowledged. If the adapter fails, the case stays pending and is retried.
- The footer says which writer produced the summary, in one line when Granite failed, plus `check_note`.
- **Suppression.** If a ring's latest decision is `dismiss` and it was made less than `SUPPRESS_HOURS` (default 24) before the new case's `written_at`, the case is acknowledged and logged, and the card gets a "dismissed" footer, but nothing new is posted. Growth of 2+ accounts or 50%+ more money compared with the dismissed version re-opens it with a new card. A ring sharing at least half its accounts with a dismissed ring gets a "related ring" note. Freeze and more_info never suppress. The rule reads `decisions`, so it survives restarts. Both clocks are wall time today; if the case writer starts sending `ring_last_seen` this could move to data time.
- **Case packet.** Header, summary, key facts, accounts with roles, transfers (each with its `txn_id`, capped at 200 rows), data lineage (the input SHA-256 from `cleaning_manifest.json` under `/data/data` when it exists), decision log and disclaimer. Every number comes from the facts pack except the transfers table, whose rows are the ring's own edges.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFIER_ADAPTER` | `console` | Only `console` exists; any other value stops with a clear message |
| `DASHBOARD_URL` | `http://localhost:8501` | Link on every card |
| `SUPPRESS_HOURS` | `24` | Suppression window for dismissed rings |
| `NOTIFIER_NAME` | `notifier` | Consumer name; run one notifier |

## Security

No ports are opened. Any future chat token belongs only in the git-ignored `.env`, never in commits, chat or screenshots. Decisions are an append-only audit stream that records who, when and through which channel. In an organisation, only the investigators' channel would be allowed to decide.
