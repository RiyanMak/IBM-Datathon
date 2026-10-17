# Mule Ring Hunter: Aryan's Laptop Data-Cleaning Plan

Updated October 17, 2026. Owner: Aryan, working locally with Claude Code.

This is an adaptation of Kevin's `Mule_Ring_Hunter_Cleaning_Blueprint.md` for one person on one laptop. It keeps the data contract and replaces the two-person assignments, parallel branches, and VM-specific working paths with a sequential local workflow.

## 1. Scope and evidence

Implement the complete data-preparation work locally: transaction cleaning, account-reference cleaning, time splits, training-only currency estimates, pattern parsing/matching, validation, and reproducible output files.

The project later replays these transactions, calculates behavioral features, trains/scores models, groups accounts, and prepares explanations. Those services are outside this cleaning assignment. Choosing a laptop for data preparation does not itself determine the wider team's final deployment location.

**Review status:** Kevin's plan was reviewed for logic and internal arithmetic. Its split totals, label totals, FX support totals, and pattern totals reconcile. The three raw datasets were not supplied with this review, so their hashes, record counts, date range, conversion rates, and pattern matches have NOT been independently verified here. Treat Kevin's measurements as reference checks until Claude reproduces them against Aryan's actual local files. This document is a plan, not evidence that cleaning has been implemented.

Use this document for local ownership, sequence, and scope. Use Kevin's blueprint for detailed reference measurements. The older 33-page helper guide remains project context; do not let its approximate ten-day description override actual source records.

## 2. Local inputs and outputs

Locate these files before assuming a directory layout:

| Input | Required for |
| --- | --- |
| `HI-Small_Trans.csv` | Transaction cleaning, chronological splits, currency estimates, pattern matching |
| `HI-Small_accounts.csv` | Account reference table and sender/receiver coverage checks |
| `HI-Small_Patterns.txt` | Evaluation-only laundering attempts and exact transaction matching |

The transaction CSV can be inspected and cleaned first if the other two files have not been downloaded. Do not claim complete account or pattern validation until their actual files have been processed. All three are required to finish this expanded blueprint.

Reuse the repository's Python environment and conventions. Inspect the README, dependencies, repository instructions, and existing code before creating a competing implementation. Preserve other teammates' changes.

Suggested output directory: `data/processed/v1/`, relative to the repository. Accept input paths and the output directory as configuration or CLI arguments; do not hard-code a Mac username, `/home/jovyan`, or `~/shared`. Raw files may remain at their existing local paths.

| Output | Purpose |
| --- | --- |
| `clean.csv` | Canonical transactions with IDs, amounts, timestamps, labels, and splits |
| `accounts_clean.csv` | Separate account reference; avoid repeating names across millions of transfers |
| `splits.json` | Derived time cutoffs, interval rules, counts, and source identity |
| `fx_rates.json` | Frozen rates fitted on training rows, with support counts and provenance |
| `patterns.csv` | Evaluation-only pattern rows matched to canonical transaction IDs |
| `data_quality.md` | Checks actually run, measured results, unresolved issues, and limitations |
| `cleaning_manifest.json` | Input hashes, schema/code version, environment, output paths, and completion status |

Keep large raw/generated files out of Git. Track source code, documentation, and small synthetic test fixtures. Tables are written as CSV, not Parquet: the LinuxONE (s390x) image has no pyarrow. A CSV keeps no column types, so read tables back with `common.clean_transactions.read_clean_csv`, which restores them.

## 3. One owner, separate responsibilities in code

Aryan owns every stage. Keep modules separate because their responsibilities differ, not because different people edit them. Adapt names to the actual repository.

| Proposed module | Responsibility |
| --- | --- |
| `src/data_contract.py` | Shared field names and normalization helpers |
| `src/clean_data.py` | Orchestrator; transaction/account preparation, ordering, splits, staged exports |
| `src/fx.py` | Fit and apply currency estimates |
| `src/patterns.py` | Parse attempt blocks, match raw records, attach canonical IDs |
| `src/validate_data.py` | Raw-to-output checks, acceptance results, report |

One task branch is sufficient. There is no Kevin/teammate integration handoff and no need for two concurrent full-dataset jobs. A separate validator and raw-based spot checks improve confidence, but they are not an independent human review.

## 4. Implement in this order

### Stage A: Inspect the repository and a small sample

Locate the inputs, identify the existing interpreter and dependency versions, and check laptop RAM/free disk. Use a 5,000-row sample to understand fields and exercise parsing. Label all sample statistics as sample-only; they do not establish full-file date ranges, class prevalence, or final split boundaries.

Check memory use before the full run. The raw files total about 510 MB according to Kevin's reference, but strings, DataFrames, sorting, and temporary copies can use several times that. Run one large preparation job at a time. Chunked reading is useful for profiling and matching, but sorting each chunk and concatenating is NOT a global sort. If RAM is insufficient, use a documented disk-backed global ordering strategy.

Do not publish a sample as `clean.csv` for evaluation. Use an explicit sample filename and mark any sample-only transaction IDs as provisional. A sample taken from the final clean file should retain its global IDs.

### Stage B: Normalize transaction records

Verify the 11 raw fields and the two Account headers. Pandas normally exposes the second as `Account.1`; check before renaming.

| Raw field | Clean field |
| --- | --- |
| `Timestamp` | `ts` |
| `From Bank` | `from_bank` |
| First `Account` | `from_acct_raw` |
| `To Bank` | `to_bank` |
| Second `Account` / `Account.1` | `to_acct_raw` |
| `Amount Received` | `amt_recv` |
| `Receiving Currency` | `ccy_recv` |
| `Amount Paid` | `amt_paid` |
| `Payment Currency` | `ccy_paid` |
| `Payment Format` | `fmt` |
| `Is Laundering` | `label` |

Rules:

- Record `source_row`, a zero-based int64 raw-record position, before sorting or filtering. Chunked input needs a global offset.
- Read bank and account identifiers as strings. Reject missing/invalid identifiers with useful diagnostics; do not turn missing values into literal account IDs.
- Canonical bank: trim outer whitespace, validate decimal digits, remove leading zeros, with an all-zero bank becoming `0`.
- Canonical account: `<canonical bank>_<account string>`. Preserve account characters and leading zeros. Use the same helper for all three files.
- Parse `ts` with `%Y/%m/%d %H:%M`. The dataset timezone is unspecified. Do not invent a timezone or append `Z`.
- Parse native amounts as finite numeric values, normally float64 for analytics. Preserve raw source tokens for exact Decimal-based pattern matching. Do not round Bitcoin to two decimals.
- Validate labels as integers in `{0,1}`. Preserve labels exactly.
- `self_transfer` is equality of the complete sender and receiver IDs. It does not imply label 0.
- Preserve valid repeated rows, self-transfers, extreme amounts, and the full observed date range. Do not trim to September 10, deduplicate, rebalance classes, or silently skip malformed rows.

### Stage C: Establish global identity and time splits

Sort the complete normalized table by `(ts, source_row)`. Assign `txn_id = 0..N-1` once after that sort. Consumers retain these IDs; they must not regenerate them inside each split.

Take the timestamps at zero-based sorted positions `floor(0.60*N)` and `floor(0.80*N)` as `validation_start` and `test_start`. Use:

- Train: `ts < validation_start`
- Validation: `validation_start <= ts < test_start`
- Test: `ts >= test_start`

Whole timestamp groups stay together, so row percentages are approximate. Equal-minute ordering is a reproducible replay convention, not known sub-minute chronology. For tiny fixtures, explicitly handle cases where timestamp boundaries would create empty splits; do not change the full-data method to make sample counts look right.

Derive these cutoffs from the actual full file. Kevin's reference values are comparison targets, not constants to blindly insert. Record boundaries and exact interval rules in `splits.json`; keep labels out of the boundary-selection logic.

### Stage D: Fit and apply FX using the derived training interval

Now that the cutoff exists, fit FX. This removes the earlier need for a second person to start from a pre-agreed hard-coded timestamp.

Use training rows only. The raw file may be unsorted, so a raw scan must examine all rows and apply the timestamp filter rather than stop at the first later row.

For each non-USD currency C, collect positive, finite candidate ratios:

| Observed transfer | USD per one unit of C |
| --- | --- |
| Paid C, received US Dollar | `amt_recv / amt_paid` |
| Paid US Dollar, received C | `amt_paid / amt_recv` |

Combine candidates in both directions and take the median per currency. Keep eligible self-transfers. Do not filter by laundering label. USD is fixed at 1.

Save full-precision rates, observation counts, spread statistics, input hash, and exclusive training cutoff. Apply the same frozen map to all splits:

`amount_usd = amt_paid * rate[ccy_paid]`

Unknown currencies or invalid conversions must be explicit failures/diagnostics, not silent fallback rates of 0 or 1. These are synthetic-data-derived estimates, not exact historical settlement values or today's exchange rates. Rounded appendix rates should be compared with a stated numeric tolerance, not exact equality.

### Stage E: Prepare account reference and the core release

Rename the accounts fields to `bank_name`, `bank_id`, `account_raw`, `entity_id`, and `entity_name`; add `account_id` with the shared helper. Preserve account/entity identifiers as strings and retain unused reference records.

Check `account_id` uniqueness and sender/receiver coverage. Save the reference separately. Any optional enrichment must use many-to-one joins with explicit sender/receiver prefixes and preserve the transaction row count. Entity IDs do not replace account IDs in the graph.

Write and reload staged clean/account files, rates, and splits. A validated core release can unblock the model and replay owners while pattern evaluation finishes. Mark its manifest as core-ready with patterns pending; do not claim full completion yet.

### Stage F: Parse and match patterns

Parse `BEGIN LAUNDERING ATTEMPT - ...` / `END LAUNDERING ATTEMPT` blocks. Reject malformed/nested/unclosed blocks. Create zero-based `attempt_id`, a separate `pattern_row_id`, source line number, and shape. Extract the shape before the colon while retaining compound names such as `SCATTER-GATHER`.

Use a full normalized transaction match key: timestamp, both bank/account pairs, both native amounts/currencies, and payment format. Exclude label from the key and verify the matched label afterward. Normalize native amount tokens exactly with Decimal; do not use rounded USD amounts or float equality as identity.

For efficiency, build the small set of pattern keys and scan the raw CSV in chunks for matches. Preserve every candidate `source_row`; do not let a dictionary silently overwrite duplicate candidates. If hashing, verify the full key before accepting a match.

Require one unambiguous raw match per pattern row, then attach final `txn_id` and `split` through `source_row`. Report missing/ambiguous cases; do not invent nearest matches. Keep attempts spanning boundaries and report their split membership.

### Stage G: Validate and finish the release

Reload actual output files using the intended reader. Produce measured pass/fail checks, not a generic PASS because files exist. Preserve raw inputs. Version outputs and publish only a coherent release; do not overwrite the version a teammate is using for training.

## 5. Output contract

Required clean columns:

- `txn_id`, `source_row`: int64.
- `ts`: datetime, timezone unspecified.
- `from_bank`, `to_bank`, `from_acct_raw`, `to_acct_raw`, `from_acct`, `to_acct`: strings.
- `amt_recv`, `amt_paid`, `amount_usd`: finite numeric amounts, unrounded during cleaning.
- `ccy_recv`, `ccy_paid`, `fmt`: category values with original meanings.
- `label`: integer 0/1, target only.
- `self_transfer`: boolean.
- `split`: train, validation, or test.

Required account columns: `account_id`, `bank_id`, `bank_name`, `account_raw`, `entity_id`, `entity_name`.

Required pattern columns: `attempt_id`, `shape`, `pattern_row_id`, `pattern_line`, `source_row`, `txn_id`, `ts`, `from_acct`, `to_acct`, `amt_paid`, `ccy_paid`, `amt_recv`, `ccy_recv`, `fmt`, `split`.

Models must use an explicit feature allowlist. Labels, true pattern shapes/membership, split indicators, and raw IDs are not predictive inputs. Account metadata initially serves lookup/display; its lack of as-of timestamps prevents assuming it was all available historically. Pattern truth remains a separate evaluation table. Clean data may retain labels for offline training; replay events must exclude them.

## 6. Acceptance checks and meaningful fixtures

Check full row/label conservation, unique and complete IDs, original row correspondence, global ordering, retained repeated records and self-transfers, account uniqueness/coverage, finite native/converted amounts, exact split intervals, FX training isolation, pattern-match uniqueness, and serialization/reload fidelity.

Use small fixtures for bank `010` versus `10`, bank `0`, account leading zeros, the same account at different banks, tied timestamps, repeated rows, a positive self-transfer, both FX directions, six-decimal Bitcoin, an unmapped currency, and ambiguous/wrong pattern matches. Check that changing valid held-out amounts cannot change fitted rates.

Perform several direct raw-to-output spot checks without simply repeating the same transformation function. Compare rounded reference rates with tolerance; compare labels, IDs, and membership exactly. Record package versions, runtime, peak memory if measured, assumptions, and unresolved issues.

## 7. Kevin's reported reference results: verify locally

Use these as expected checks only for byte-identical inputs. A different hash requires fresh profiling and documented results, not editing the data to force agreement. An unexplained mismatch with the same hash requires investigation before publication.

| Check | Reported result |
| --- | --- |
| Transaction rows | 5,078,345 |
| Positive / normal labels | 5,177 / 5,073,168 |
| Date range | 2022-09-01 00:00 through 2022-09-18 16:18 |
| Rows after September 10 / positives among them | 1,108 / 655 |
| Self-transfers / positive self-transfers | 591,212 / 11 |
| Identical source-row pairs retained | 9 pairs |
| Account reference rows / unique keys | 518,581 / 518,581 |
| Active transaction accounts / unused reference accounts | 515,088 / 3,493 |
| Missing account-reference endpoints | 0 |
| Currencies / payment formats | 15 / 7 |
| Foreign-currency FX evidence | 34,074 training observations across 14 currencies |
| Pattern attempts / rows | 370 / 3,209 |
| Unique / ambiguous / missing pattern matches | 3,209 / 0 / 0 |

| Split | Reported rule | Rows | Positives |
| --- | --- | --- | --- |
| Train | `ts < 2022-09-06T13:36:00` | 3,046,861 | 2,297 |
| Validation | `2022-09-06T13:36:00 <= ts < 2022-09-08T16:12:00` | 1,015,602 | 1,082 |
| Test | `ts >= 2022-09-08T16:12:00` | 1,015,882 | 1,798 |

Reported SHA-256 identities:

| Input | SHA-256 |
| --- | --- |
| `HI-Small_Trans.csv` | `b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040` |
| `HI-Small_accounts.csv` | `786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014` |
| `HI-Small_Patterns.txt` | `2c546b5ce6009e73851f0139af053cf845f08bf92f3bc82fe1eb937dec2ef39b` |

Kevin's appendix contains the reported currency rates and detailed pattern-shape counts. Recompute those; do not copy them into production outputs as if fitted locally.

## 8. Handoff and limitations

Tell consumers the release path/version, exact schema, cutoff rules, and validation status. Provide portable scripts and configuration so a later LinuxONE run can use different paths without rewriting cleaning logic. Local cleaning uses the local Python environment; Docker/SSH are not prerequisites for this task.

Tell the feature/replay owner to warm up account history from earlier events without labels before test replay. A mid-test demo also needs state from before its start. New-payee and account-age history may require more than a 24-hour warmup. Keep feature computation ordered and causal.

The patterns file is incomplete ground truth: Kevin reports 3,209 of 5,177 positives covered, with 155 attempts spanning splits. Missing pattern membership does not establish a legitimate transfer or a false-positive ring. Keep all transaction labels and distinguish wholly test attempts from cross-boundary ones in later evaluation. The reported late-date tail has unusually many positives; preserve it and report it transparently without tuning on test results.

## 9. Instructions for Claude Code

Work with Aryan as the sole cleaning owner on his laptop. Explain each stage's inputs, transformations, outputs, and checks in plain English, then implement that stage in the existing repo. Use small examples so Aryan can understand the changes. Avoid one large unexplained script or unrelated services.

First inspect the repository and input locations. Report what exists, what has already been implemented, and any missing input files. Read a small transaction sample and check the local environment. Compute file hashes without loading entire files into memory. Reuse completed work rather than starting over.

Next implement the core transaction/split/FX/account workflow, verify it, and run it on the complete local input with a memory-conscious plan. Then finish patterns and final validation. Keep checkpoints visible; do not repeatedly ask for permission for routine changes already covered by this assignment. Raise a focused question only for a genuine unresolved data-contract conflict or missing required information.

The final result must include runnable commands, actual output paths, measured checks, and an honest list of any incomplete stages. Do not claim Kevin's measurements were reproduced until the local run establishes them.
