# Step 5: Case writer (ring → plain-English case)

**Owner:** Ayan · **Reads:** Redis stream `rings` (from step 4) · **Writes:** Redis stream `cases` (to step 6, the page)

When the ring service finds a ring, this piece turns it into a short case an investigator can read in 20 seconds:

```
ring message  →  1. facts pack  →  2. template summary (always works)
                                →  3. Granite summary (optional)  →  4. fact check
                                                                        pass → use Granite
                                                                        fail → use template
              →  case message on the `cases` stream
```

Our code computes every number. Granite only rewords them, and the fact check throws out any reply that contains a number, account or date that isn't in the facts.

---

## What's in this folder

| File | What it does | Do you edit it? |
|---|---|---|
| `facts.py` | Reads a ring and computes the facts (amounts, % passed on, hours, key account, collector) | Rarely |
| `template.py` | The fill-in-the-blanks summary, one description per laundering shape | **Yes, this is your wording** |
| `factcheck.py` | The checker that rejects invented numbers, accounts and dates | No |
| `granite.py` | The prompt and the call to Granite on watsonx.ai | Only if the lab connects differently |
| `casewriter.py` | Ties everything together; runs on a file or on Redis | No |
| `fake_rings.json` | 3 hand-made rings (scatter-gather, fan-out, cycle) for testing | Add more if you like |
| `patterns_to_rings.py` | Turns real laundering attempts from the dataset into rings, for testing on real data | No |
| `main.py` | What `docker compose` runs: `python -m services.casewriter.main` | No |
| `test_casewriter.py` | 16 automatic checks | No |

Shared with the other services (not in this folder): `common/bus.py` (Redis host and stream names), the root `requirements.txt`, `Dockerfile`, `docker-compose.yml` and `.env.example`. Copy the root `.env.example` to `.env` for your watsonx settings; `.env` is git-ignored.

Exchange rates come from the cleaning pipeline: `data/processed/fx_rates.json`, written by `python -m scripts.clean_data` (see the root README). That is the team's only rate table.

This piece never reads the Kaggle files. It only reads rings, so the dataset choice (HI-Small) doesn't affect it.

**Every command below runs from the repo root**, not from inside this folder.

---

## Step 1: Get the code (2 min)

```
cd path/to/your/repo
git pull
python3 -m pip install -r requirements.txt
```
The case writer is in `services/casewriter/`. Check that there is **no `.env` file** committed anywhere (only `.env.example` at the repo root).

## Step 2: Run the tests on your Mac (2 min)

```
python3 -m pytest -q services/casewriter
```
or, for the one-line-per-test view:
```
python3 -m services.casewriter.test_casewriter
```
Every line should say `PASS`, ending with `All tests passed.` One test may say `SKIP` (it needs `fakeredis`). That's fine. `python3 -m pytest -q` at the repo root runs these together with the cleaning tests.

Rerun this after **every** edit you make. If something says `FAIL`, your edit broke a rule. Undo it or ask.

## Step 3: See what the cases look like (2 min)

```
python3 -m services.casewriter.main --file services/casewriter/fake_rings.json
```
This prints the 3 fake rings as finished cases. Each one has 5 parts: `summary`, `what_happened`, `why_suspicious`, `possible_victim`, `suggested_next_step`. That output is exactly what the investigator page will show.

## Step 3b: Test on REAL laundering attempts from the dataset (10 min)

The fake rings are made up. To test on real data before the ring service exists, convert real attempts from the dataset's patterns file into rings. Run this once the cleaning pipeline has produced `fx_rates.json` (root README, "Data cleaning"):
```
python3 -m services.casewriter.patterns_to_rings ~/Downloads/HI-Small_Patterns.txt real_rings.json --rates data/processed/fx_rates.json --limit 40
python3 -m services.casewriter.main --file real_rings.json
```
- Without `--rates`, only attempts paid entirely in US Dollars are used. Inside the containers the defaults are `/data/HI-Small_Patterns.txt` and `/data/fx_rates.json`, so both arguments can be left out there.
- The rates are learned from the training split only, by the same code the scorer uses, so test rings and the scorer agree on every dollar amount. Do not build a second rate table.
- These rings have no model scores yet, so the score sentences are skipped. That's expected.
- Read 5–10 of the summaries. Do they make sense for the shape? Do the amounts look right? Note any that read badly, and fix the wording in step 4.
- Pick 2–3 that read well as **demo moments** for the video. The team guide says Pair 1 should pick demo attempts, so share yours with them.

## Step 4: Your finance job, the wording (30–60 min)

Open `template.py` in any text editor (VS Code or TextEdit in plain-text mode).

- `SHAPE_TEXT` has one plain-English sentence per laundering shape. Make them sound like a real investigator wrote them.
- `template_summary()` has the five parts. Change the sentences inside the quotes, and keep the `{...}` placeholders. Those are where the numbers go.
- **Rules:**
  1. Only use numbers from the facts (the `f[...]` values). Never type a number into a sentence.
  2. Say "activity consistent with", never "criminal", "guilty" or "fraudster".
  3. The key account is treated as a **possible victim** (a recruited mule). That's our student angle.
- Save, then run `python3 -m pytest -q services/casewriter` again. Commit when it passes.

The terms the template uses, so you can explain them to judges:

| Fact | Meaning |
|---|---|
| `key_account` | The account that passed the most money straight through (received it, then sent it on). The likely mule. |
| `passed_on_pct` | Money the key account sent on ÷ money it received, as a % |
| `hours_to_pass_on` | Time from the key account's first incoming transfer to its first outgoing one |
| `origin` / `collector` | Where most money started / ended. Only named if it's at least 25% of the ring's money |
| `xgb_score` / `gnn_score` | The two models' scores for the ring (0 to 1) |
| `top_reasons` | The top 3 reasons the scorer gave |

## Step 5: Lock the handoffs with the team (10 min, do this now)

Paste this in the team chat so Pair 2 (rings) and Pair 3 (page) build to the same format:

> **Case writer contract (Ayan).**
> I read `rings` with consumer group `casewriter`. Fields as in the guide: `ring_id, version, shape, n_accounts, accounts, edges, xgb_score, gnn_score, reasons, first_seen, last_seen`, all as text, lists as JSON.
> **Pair 2:** each edge can be a dict `{"txn_id","from","to","amount_usd","ts","score"}` or a list in that order. Times as `2022-09-08T14:00:00`. Account IDs as `10_8000EBD30`. Please confirm which one you'll send.
> **Pair 3:** I write `cases` with the guide's fields: `ring_id, version, summary (JSON with summary, what_happened, why_suspicious, possible_victim, suggested_next_step), source (granite/template), check_note, written_at`. I also add one extra field, `facts` (JSON), in case the page wants to show the numbers. Ignore it if not.

## Step 6: Connect Granite (when you have credentials)

1. **Get the details.** You need an API key, project ID and region URL. Ask in the Discord IBM channel, or check how the mandatory "Predictive AI + Generative AI" lab connects. **The guide says to use the lab's method.** If the lab uses a different method from `granite.py`, only the inside of `call_granite()` needs replacing.
2. **Make your settings file** (at the repo root):
   ```
   cp .env.example .env
   ```
   Open `.env` and paste in the real values (`WATSONX_APIKEY`, `WATSONX_PROJECT_ID`, `WATSONX_URL`, `GRANITE_MODEL_ID`). Granite model names change, so check which ones your watsonx project lists.
3. **Test it:**
   ```
   python3 -m services.casewriter.main --file services/casewriter/fake_rings.json
   ```
   Each ring now prints `source: granite` or `source: template` with a reason. The last line, `Granite passed the fact check in X of Y tries`, goes in the pitch.
4. **If it always says `granite error`,** read the error text. A `401` means the key is wrong, `404` means a wrong URL or model ID, and a timeout means the VM can't reach watsonx (ask Pair 3). The template still works, so nothing is blocked.

I couldn't test the real Granite connection from here, only the request format. Step 6 is where you find out whether it works.

## Step 7: Run it on the VM with Redis (once Pair 3 has Redis up)

On the LinuxONE VM, at the repo root:
```
export REDIS_HOST=localhost          # outside docker compose; inside it the default "redis" is right
python3 -m services.casewriter.main --push-fake services/casewriter/fake_rings.json   # put 3 fake rings on the stream
python3 -m services.casewriter.main                                                   # start the service
```
You should see `case for R-0001 v1 (template)` and so on. Press **Ctrl + C** to stop. To check what landed on `cases`:
```
redis-cli XRANGE cases - +
```
(or `docker exec -it <redis-container> redis-cli XRANGE cases - +` if Redis runs in a container)

This proves your piece works before Pair 2's real rings exist. When their ring service is running, the same `--redis` command picks up real rings automatically.

## Step 8: Run it in the shared containers

The root `docker-compose.yml` already has a `casewriter` service that runs `python -m services.casewriter.main` from the shared image, with `.env` passed in and Redis reachable as `redis`:
```
docker compose up -d --build casewriter
docker compose logs -f casewriter      # expect: case writer listening on 'rings'
```

## Step 9: Done when (from the team guide)

- [ ] Every ring gets a case, from Granite or the template
- [ ] A deliberately wrong reply is caught (already covered by `test_lying_granite_reply_falls_back`; show it in the video if there's time)
- [ ] The page shows which source wrote each summary
- [ ] You have the pitch number: "Granite passed the fact check in X of Y cases; the rest fell back to the template"

---

## If a judge asks "how do you stop the AI making things up?"

"Granite never sees raw data and never makes decisions. Our code computes every number into a facts pack. Granite rewrites those facts in plain English, and then a deterministic checker pulls every number, account ID and date out of the reply and checks it against the facts. One mismatch and we discard the reply and use a template written from the same facts. So the investigator always gets a correct summary, and the screen shows which one wrote it."

## When something goes wrong

| What you see | What it means | Fix |
|---|---|---|
| `python3: command not found` | Python isn't installed on your Mac | Run `xcode-select --install`, or just use the VM |
| `ModuleNotFoundError: redis` / `requests` | Library not installed | `python3 -m pip install -r requirements.txt` at the repo root |
| `ModuleNotFoundError: services` / `common` | You're not at the repo root | `cd` to the repo root; always run `python3 -m services.casewriter...` from there |
| A test says `FAIL` after you edited the wording | A sentence now contains a number that isn't from the facts, or a part is empty | Undo the last change; use `{...}` placeholders for numbers |
| `skipped bad ring message ... Unrecognised edge` | Pair 2 sends edges in a different format | Ask them for one example message and post it in the team chat |
| `Connection refused` on Redis | Redis isn't running or `REDIS_HOST` is wrong | Inside compose it is `redis` (default); on a laptop `export REDIS_HOST=localhost` |
| `source: template, granite error: 401` | Wrong API key | Re-copy the key into `.env` |
