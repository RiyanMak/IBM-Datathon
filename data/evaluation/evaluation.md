# Ring-level evaluation

## Run parameters

| Parameter | Value |
|---|---|
| patterns | `/data/data/HI-Small_Patterns.txt` |
| rates | `/data/data/fx_rates.json` |
| splits | `/data/data/splits.json` |
| since | `2022-09-08T16:12:00` |
| rings from | `redis stream 'rings'` |
| ring versions read | `1138` |

## Headline numbers

| Measure | Value |
|---|---|
| Known attempts starting in the period | 95 |
| Caught by a ring | 58 (61%) |
| Covered (half the attempt's accounts in one ring) | 42 |
| Shape match rate at the first alert, among caught | 16% |
| Median minutes from first transfer to alert | 501 |
| 90th percentile minutes to alert | 2634 |
| Median share of the money still stoppable at the alert | 81% |
| Attempts that started before the period (reported apart), caught | 97, 64 |

## Against the known attempts (HI-Small_Patterns.txt)

An attempt is caught when at least half of some ring's accounts belong to it; the first ring version that does so sets the alert time. The headline denominator is the attempts whose first transfer lies in the replayed period. Attempts that started earlier and were still running are reported on their own line, because the replay only showed their tail.

| Measure | Value |
|---|---|
| Known attempts starting in the period | 95 |
| Caught by a ring (half the ring's accounts belong to the attempt) | 58 (61%) |
| Covered (half the attempt's accounts in one ring) | 42 |
| Shape name matches at the first alert, among caught | 9 (16%) |
| Shape name matches in the ring's latest version | 28 (48%) |
| Minutes from first transfer to alert, median / mean / 90th percentile | 501 / 876 / 2634 |
| Money still stoppable at the alert, median / mean | 81% / 75% |
| Money still stoppable, total over caught attempts | $272,522,849 of $346,942,079 |
| Rings published (distinct ids) | 393 |
| Rings matching a known attempt | 153 |
| Attempts that started before the period (reported apart), caught | 97, 64 |

| Shape | Attempts | Caught | Shape matched at alert | Shape matched, latest |
|---|---|---|---|---|
| bipartite | 17 | 3 | 0 | 0 |
| cycle | 10 | 8 | 0 | 1 |
| fan-in | 12 | 9 | 1 | 8 |
| fan-out | 10 | 8 | 0 | 7 |
| gather-scatter | 16 | 13 | 0 | 2 |
| other | 12 | 8 | 8 | 7 |
| scatter-gather | 11 | 6 | 0 | 3 |
| stack | 7 | 3 | 0 | 0 |

6 alert(s) came before the attempt's first transfer (the ring formed from other activity of the same accounts); those count as 0 minutes.

### Fastest and slowest alerts

| Attempt | Shape | Ring | Minutes to alert | Money still stoppable |
|---|---|---|---|---|
| A-0289 | fan-in | R-0018 | 0 | 100% |
| A-0296 | cycle | R-0016 | 0 | 100% |
| A-0313 | fan-out | R-0018 | 0 | 100% |
| A-0342 | other | R-0016 | 0 | 100% |
| A-0345 | gather-scatter | R-0016 | 0 | 100% |
| … | | | | |
| A-0302 | bipartite | R-0354 | 2738 | 0% |
| A-0327 | other | R-0369 | 2768 | 0% |
| A-0337 | cycle | R-0382 | 3404 | 23% |
| A-0283 | fan-out | R-0361 | 3962 | 51% |
| A-0291 | other | R-0376 | 4696 | 20% |

## Limitations

- The patterns file is an incomplete answer key: it names 3,209 of the 5,177 laundering transfers, so a ring that matches no attempt is not shown to be a false alarm.
- Times are data time (September 2022), not wall-clock time during the replay. Transfers in the same minute have no order inside that minute, and the alert time is the regroup that published the ring.
- The alert time is the first ring version that caught the attempt, when a person could first have acted; later versions may add accounts and change the shape name (both columns are reported).
