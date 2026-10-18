# Sharpen the Booster, Shelve the GNN

**Your XGBoost already matches IBM's own published gradient-boosting result on HI-Small.** Test minority-class F1 of 0.63 equals GFP+XGBoost under the same temporal 60/20/20 protocol: 63.23 at batch size 128 and 64.77 at batch 2048 ([NeurIPS 2023 dataset paper](https://arxiv.org/html/2306.16424v3); [GFP paper](https://arxiv.org/html/2402.08593v2)). It is at or above every plain GNN in the benchmark papers (GIN 28.7 to 46.5, PNA 56.8 to 63.0). A few boosting runs score higher only under looser protocols: 64.77 at GFP batch 2048, and 67.7 on a non-chronological split with a tuned threshold ([AMLworld-Compact](https://arxiv.org/html/2610.13627)). Every other published score above yours comes from a GPU-trained graph model (Multi-PNA+EU 68.2, FraudGT 69.7, MEGA-PNA 74.0) or from a preprint whose method the research did not capture (ExSTraQt 78.9) ([Multi-GNN](https://arxiv.org/abs/2306.11586); [MEGA](https://arxiv.org/html/2412.00241v3); [ExSTraQt](https://arxiv.org/html/2604.02899v1)). Even the lighter Multi-GIN took **3.6 hours on an RTX 3090** to train, so none of these is reachable on three s390x cores this afternoon. The best remaining moves are all inside the booster, in this order:

1. Pick the decision threshold on the validation period and freeze it. Precision 0.85 against recall 0.50 says the current operating point is lopsided.
2. Add the two feature families IBM's ablation shows supply the last 5 to 12 points on top of fan counts: short temporal cycles and per-account distribution statistics.
3. Try a second-pass "taint" feature that feeds first-stage scores of upstream transfers back into the model. It targets the **38% of HI-Small laundering that belongs to no pattern**, which the simulator labels transitively downstream.

Expect a few F1 points, not ten. About **36% of test positives fall in a tail after 10 September, when legitimate traffic nearly stops**, and that tail flatters every model's score ([hecmeynell repo](https://github.com/hecmeynell-debug/aml-project)). Report results split around that date and per laundering pattern. Treat the small GNN as a demo-scale re-scorer on candidate rings, not as a route to a higher F1.

## Six moves ranked by F1 points per hour

The table ranks what to do with the hours left. **The "expected effect" and "cost" columns are my estimates from the cited evidence.** No source measured these exact changes on your pipeline. The estimates assume your test set is the last 20% of HI-Small by timestamp, which is the protocol IBM used.

| Rank | Move | Anchor evidence | Expected test-F1 effect (estimate) | Cost on 3 cores (estimate) | Main risk |
|---|---|---|---|---|---|
| 1 | Choose the F1-maximising threshold on the validation period, freeze it, apply once to test | Validation-thresholded XGBoost+GFP reached P 81.7 / R 57.8 / F1 67.7 on HI-Small (non-chronological split); threshold tuning beat resampling and class weights across 30 datasets | +0 to +3 points; PR-AUC unchanged | Under 30 min | Test prevalence (about 0.18%) is roughly double that of the earlier 80% of rows (about 0.08%) |
| 2 | Fix the yardstick: 3 seeds, average precision, test split before/after 11 Sep, recall per laundering pattern, precision@K | Published std runs up to ±8 points; one LightGBM scored PR-AUC 0.968 after 11 Sep and 0.407 before | 0 points, but protects every claim you make | 30–45 min | None |
| 3 | Add temporal 2- and 3-cycles, 6-hour scatter-gather counts, degree-vs-fan ratios, and amount/time moments (median, variance, skew, kurtosis) for both endpoints in both directions | On top of fan features, multi-hop patterns added 1.3–5.8 and vertex statistics 3.8–8.2 F1 points on HI-Small; in a SHAP example the top two features were 2-hop temporal cycles and money received | +1 to +4 points | 1–3 h coding, minutes to retrain | Overlap with your 32 features; leakage if not computed strictly from the past |
| 4 | Second-pass "taint" features built from cross-fitted first-stage scores of earlier inbound transfers | Random-walk "distance to known illicit" features added 13.4 recall points at fixed FPR at a real bank; the pseudo-label variant added 6.4; AMLworld labels propagate downstream | Unknown on HI-Small; plausibly +2 to +5 | 2–3 h | In-sample scores leak labels and collapse on test |
| 5 | Light tuning: scale_pos_weight 1–10, early stopping on aucpr, 3 seeds, average XGBoost with LightGBM | Hyperparameter tunability is small for XGBoost; a two-booster ensemble gave 67.8 vs 67.7 F1 with tighter variance | +0 to +1 point | 30–60 min of compute | Overfitting the validation period |
| 6 (optional) | IBM Snap ML Graph Feature Preprocessor (GFP) for multi-hop features | s390x wheels exist; about 30 ms per 128-transaction batch on 64 threads | +0 to +3 points, overlapping move 3 | 30 min install, then roughly 2–7 h of compute by linear extrapolation | Runtime on 3 cores is unmeasured |
| Last | Small GNN | Multi-GIN needed 3.6 GPU-hours; PyG's sampling libraries have no s390x wheels | Not expected to beat XGBoost | Many hours | Infeasible on this hardware in time |

**Move 1 is the cheapest real gain.** IBM's published F1 numbers use a fixed 0.5 cut-off ([NeurIPS paper](https://arxiv.org/html/2306.16424v3)). The Multi-GNN code thresholds at argmax and reports the test F1 from the epoch with the best validation F1 ([Multi-GNN training.py](https://raw.githubusercontent.com/IBM/Multi-GNN/main/training.py)).

A study of 73 tabular datasets found that oversampling never improved XGBoost by more than one standard deviation ([Elor & Averbuch-Elor](https://arxiv.org/html/2201.08528v3)). For strong boosters, the best F1 came from **optimising the threshold on validation data with no rebalancing at all**, though its margin over rebalancing was not significant. Across 9,000 experiments, threshold calibration was the most consistent fix ([Balancing the Scales](https://arxiv.org/html/2409.19751v1)). It lifted XGBoost from 0.619 to 0.637 and LightGBM from 0.616 to 0.650. A 2026 study of 45 tasks found the benefit peaks at +0.12 F1 for imbalance between 1:15 and 1:40, then falls to +0.045 beyond 1:100 ([45-task study](https://arxiv.org/abs/2608.16147)). Its most extreme task was 1:178. HI-Small is 1:981, so a modest gain is the honest expectation. IBM's own authors also note that for XGBoost on this data, raising recall without "significantly degrading the precision" is hard ([NeurIPS paper](https://arxiv.org/html/2306.16424v3)).

Tune on the validation predictions only. One practical route is scikit-learn's `TunedThresholdClassifierCV` with `cv="prefit"` on the validation set. Its documentation warns that tuning on the data the model was fit on overfits ([scikit-learn](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TunedThresholdClassifierCV.html)). Never tune on test. The one HI-Small paper that chose its ensemble threshold on test labels labelled the result "exploratory" ([AMLworld-Compact](https://arxiv.org/html/2610.13627)).

One wrinkle comes from my own arithmetic on two papers' counts. The last 20% of rows holds 1,797 of the 5,177 positives, about 0.18%, against roughly 0.08% earlier ([BlazingAML confusion matrix](https://arxiv.org/html/2604.12241v1); [GARG-AML pattern table](https://arxiv.org/html/2506.04292)). Precision rises with prevalence at a fixed threshold ([Brabec et al.](https://arxiv.org/abs/2001.05571)). So a threshold tuned on validation will probably sit a little conservative for test. That is the price of an honest protocol.

**Move 2 makes the number defensible.** On HI-Small, published standard deviations run from ±0.17 for GFP+XGBoost to ±7.9 for GIN+EU ([NeurIPS paper](https://arxiv.org/html/2306.16424v3); [Multi-GNN](https://arxiv.org/abs/2306.11586)), so average at least three seeds. Report PR-AUC as average precision. scikit-learn notes that the trapezoidal area "can be too optimistic" ([scikit-learn](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.average_precision_score.html)).

Slice the test set at 11 September. A temporal LightGBM on the same split scored F1 0.426 and PR-AUC 0.407 before that date, but **0.769 and 0.968 after it**. Overall it scored 0.567 and 0.587, with precision@100/500/1000 of 0.99/0.98/0.83 ([hecmeynell repo](https://github.com/hecmeynell-debug/aml-project)).

Report recall per laundering pattern from the patterns file, so judges can compare against Multi-PNA's per-pattern recall on Small HI ([Multi-GNN](https://arxiv.org/abs/2306.11586)):

| Pattern | Multi-PNA recall |
|---|---|
| Fan-in | 85 |
| Fan-out | 93 |
| Scatter-gather | 94 |
| Gather-scatter | 93 |
| Cycle | 62 |
| Bipartite | 55 |
| Not in any pattern | 0.5 |

**Move 3 adds what IBM's preprocessor computes and your features likely lack.** In IBM's ablation on HI-Small, XGBoost rose from 19.75 with basic features to 56.88 with fan-in/fan-out counts, then to 58.60 with multi-hop cycles and scatter-gather, then to 63.23 with vertex statistics ([GFP paper, Table 5](https://arxiv.org/html/2402.08593v2)). LightGBM gained 8.2 points from vertex statistics alone. Your distinct-payee and distinct-payer counts already cover fan-in/fan-out, which explains why you sit at 0.63. The missing pieces are cheap to compute inside a causal 24-hour window. The first is temporal 2-cycles: A pays B, then B pays A. In the GFP paper's SHAP example for one flagged HI-Small transaction, the two most important features were the number of two-hop temporal cycles and the sum the target account received. The others are scatter-gather counts over 6 hours (IBM's window), a degree-to-fan ratio, and higher moments of amount and inter-arrival time.

**Move 4 is the high-upside bet.** AMLworld marks laundering **transitively**. If A pays B $100 of illicit funds and B forwards $50 to C, both transfers are labelled ([NeurIPS paper](https://arxiv.org/html/2306.16424v3)). Many "unpatterned" positives are therefore downstream of something you already score highly.

The design that follows is my synthesis, not a tested recipe. Train stage 1 on two halves of the training period, each scoring the other half (cross-fitting; general methodology, not from the research). For every transaction, compute from earlier transactions only:

- the maximum and mean stage-1 score of transfers into the sender over the past 24 hours;
- the score-weighted share of the sender's inflow;
- the same quantities for the receiver.

Stage 2 is XGBoost on your features plus these taint features. Evidence that this family works comes from outside HI-Small. Feedzai's random-walk "distance to illicit" features added 13.4 recall points at 20% FPR at a real bank, and a variant using model pseudo-labels instead of true labels still added 6.4 ([Naser Eddin et al.](https://arxiv.org/abs/2112.07508)). The Kaggle fraud winners replaced each client's predictions with the client's average ([NVIDIA on the IEEE-CIS 1st place](https://developer.nvidia.com/blog/leveraging-machine-learning-to-detect-fraud-tips-to-developing-a-winning-kaggle-solution)). Your ring service already does a post-hoc version of this; the step is turning it into a model input.

**Moves 5 and 6 are polish.** IBM's GBDT searches used scale_pos_weight 1–10, depth 1–15, learning rate 10^−2.5 to 10^−1, up to 1,000 rounds, and early stopping after 20 rounds ([GFP paper](https://arxiv.org/html/2402.08593v2)). A public HI-Small LightGBM used scale_pos_weight = (neg/pos)^0.25 ≈ 6 ([hecmeynell repo](https://github.com/hecmeynell-debug/aml-project)). Tuning XGBoost's hyperparameters yields only about 0.043 AUC over package defaults and 0.014 over good defaults ([Probst et al.](https://arxiv.org/abs/1802.09596)).

On HI-Small, XGBoost+GFP scored 67.7 ± 0.3, LightGBM+GFP 65.7 ± 1.2, and their ensemble 67.8 ± 0.2 at higher precision (84.3) ([AMLworld-Compact](https://arxiv.org/html/2610.13627)). Averaging boosters therefore buys stability more than points. For the 3-core VM, set threads to the real core count and avoid running experiments in parallel or copying data, which "usually consumes a lot more memory than people expect" ([LightGBM docs](https://lightgbm.readthedocs.io/en/latest/Parameters.html); [XGBoost tuning guide](https://xgboost.readthedocs.io/en/stable/tutorials/param_tuning.html)).

## Your 0.63 already sits on IBM's published boosting line

The HI-Small leaderboard spans 20 to 81 F1, but much of that spread comes from protocol and hardware rather than modelling insight.

| Approach | HI-Small minority F1 (%) | Training hardware | Source |
|---|---|---|---|
| XGBoost / LightGBM, basic features only | 19.75 / 21.30 | CPU | [GFP paper](https://arxiv.org/html/2402.08593v2) |
| GIN (three different runs) | 28.70 / 40.04 / 46.50 | GPU | [NeurIPS](https://arxiv.org/html/2306.16424v3), [FraudGT](https://jshun.csail.mit.edu/FraudGT.pdf), [MEGA](https://arxiv.org/html/2412.00241v3) |
| PNA (three runs) | 56.77 / 61.06 / 62.96 | GPU | same three |
| Multi-GIN (three runs) | 57.15 / 47.42 / 62.66 | GPU | [Multi-GNN](https://arxiv.org/abs/2306.11586), FraudGT, MEGA |
| GFP + LightGBM, batch 128 / 2048 | 62.86 / 60.52 | 64 CPU threads | [GFP paper](https://arxiv.org/html/2402.08593v2) |
| GFP + XGBoost, batch 128 / 2048 | 63.23 / 64.77 | 64 CPU threads | [GFP paper](https://arxiv.org/html/2402.08593v2) |
| **Your XGBoost (P 0.85, R 0.50)** | **63** | **3 s390x cores** | — |
| Multi-PNA+EU | 68.16 ± 2.65 | GPU | [Multi-GNN](https://arxiv.org/abs/2306.11586) |
| FraudGT | 69.68 ± 1.58 | V100 | [FraudGT](https://jshun.csail.mit.edu/FraudGT.pdf) |
| SALT-Trans | 71.6 ± 1.8 | RTX A6000 | [SALT-GNN](https://arxiv.org/html/2607.10131v1) |
| MEGA-PNA | 74.01 ± 1.55 | RTX 4090 | [MEGA](https://arxiv.org/html/2412.00241v3) |
| Multi-FraudGT | 75.81 ± 0.75 | RTX 4090 run | [MEGA](https://arxiv.org/html/2412.00241v3) |
| ExSTraQt (GFP split / FraudGT split) | 78.90 / 80.89 | Apple M3 Pro, 12 CPU cores | [ExSTraQt](https://arxiv.org/html/2604.02899v1) |

**Three protocol details decide whether a comparison is fair.**

*The split.* IBM's GBDT protocol takes the first 60% of transactions by timestamp, then the next 20%, then the last 20% ([GFP paper](https://arxiv.org/html/2402.08593v2)). The Multi-GNN code instead cuts at whole-day boundaries, which works out to **64/19/17** on HI-Small, the split FraudGT and MEGA report ([Multi-GNN data_loading.py](https://raw.githubusercontent.com/IBM/Multi-GNN/main/data_loading.py); [FraudGT](https://jshun.csail.mit.edu/FraudGT.pdf)). AMLworld-Compact split the CSV in file order, so its timestamp ranges overlap ([AMLworld-Compact](https://arxiv.org/html/2610.13627)). BlazingAML used 80/20 and included account IDs as features ([BlazingAML](https://arxiv.org/html/2604.12241v1)). IBM excludes account IDs precisely to stop models memorising launderers.

*The threshold.* Most published rows use 0.5 or argmax. A validation-tuned threshold is legitimate but should be disclosed.

*Variance.* Reruns of the same GNN differ by nearly 18 points (GIN 28.7 versus 46.5), so a 3-point gap between GNN rows is noise.

On the protocol closest to yours, the honest summary is that **your model ties GFP+XGBoost and trails the best adapted GNNs by 5 to 13 points**. There is one caveat on GFP batch size. The paper says batch "∞" is effectively offline because "the future transactions are also visible during feature extraction" ([GFP paper](https://arxiv.org/html/2402.08593v2)). Larger finite batches such as 2048 likely let each transaction see more of its batch-mates. Your strictly past-24-hour features are the honest analogue of batch 128.

**Two dataset quirks shape what is achievable.**

*The late tail.* The Kaggle page describes its date range as the "primary" period. In a discussion with a user, the maintainers reportedly said the transactions after it are all laundering ([Kaggle dataset](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml)). One notebook log shows dates running 1 to 18 September 2022, with 9 duplicate rows ([Kaggle notebook](https://www.kaggle.com/code/ardabaranbaytar/anti-money-laundering-transaction-analysis-and-ris/log)). From 11 September, 58–73% of transactions are laundering, because "licit activity almost stops" while laundering continues ([hecmeynell repo](https://github.com/hecmeynell-debug/aml-project)). A model can learn "little activity around" as a signal. Do not add global-activity features that exploit this, and say so in the pitch.

*Pattern coverage.* Of HI-Small's 5,177 laundering transactions, **1,968 (38%) match none of the eight typologies** ([GARG-AML](https://arxiv.org/html/2506.04292); [Multi-GNN](https://arxiv.org/abs/2306.11586)):

| Typology | Laundering transactions |
|---|---|
| Gather-scatter | 716 |
| Scatter-gather | 626 |
| Stack | 466 |
| Fan-out | 342 |
| Fan-in | 318 |
| Cycle | 287 |
| Bipartite | 263 |
| Random | 191 |
| None of the eight | 1,968 |

Even Multi-PNA recalls only 0.5% of these unpatterned transactions. In a CSV-order test partition, 37% of illicit edges carried no typology label ([AMLworld-Compact](https://arxiv.org/html/2610.13627)). By that arithmetic, a pure pattern detector tops out around 63% recall. ExSTraQt reports 70.2% recall, which shows some unpatterned laundering is catchable, most plausibly through the transitive downstream labels that Move 4 targets ([ExSTraQt](https://arxiv.org/html/2604.02899v1)).

The NeurIPS paper's Table 4 lists HI-Small with 3.6K laundering transactions over 10 days. Its own rate of 1 in 981 implies about 5.2K, and the CSV-based counts above match that ([NeurIPS paper](https://arxiv.org/html/2306.16424v3)).

## Cycles and distribution statistics carry the last points; resampling carries none

**The feature evidence is consistent across three independent sources.** IBM's ablation shows fan-in/fan-out counts add 28 to 40 F1 points over basic features. The text says cycles and scatter-gather add "up to 4%", though its table shows +1.3 to +5.8 points depending on model and batch size, and vertex statistics add the rest ([GFP paper](https://arxiv.org/html/2402.08593v2)).

BlazingAML reproduced the ordering with XGBoost on HI-Small: 23.2 with fan, 38.4 with degree added, 42.6 with cycles, and 46.6 with scatter-gather. Its authors conclude the contribution hierarchy is "Scatter-Gather > Cycle > Degree > Fan". Its weaker absolute numbers reflect an ID-laden baseline and an 80/20 split ([BlazingAML](https://arxiv.org/html/2604.12241v1)).

At a real bank, neighbourhood degree features (mean, minimum and maximum degree of an account's counterparties) added 11.6 recall points at 20% FPR ([Naser Eddin et al.](https://arxiv.org/abs/2112.07508)). The same work used profile windows of 1 day, 1 and 2 weeks, and 1 and 2 months. It found a 1-day memory of legitimate events optimal, with suspicious-event memory paying off up to 20–30 days.

A public temporal HI-Small LightGBM reached F1 0.567 with no cycle or scatter-gather features, only windowed counterparty counts and volumes. It used 1-, 7- and 30-day windows, and its strongest inputs were whether the sender-receiver pair had transacted before, the sender's recent inflow, payment format and amount ([hecmeynell repo](https://github.com/hecmeynell-debug/aml-project)). Your 0.63 already beats it.

Rebuilding GFP-style features is not trivial. One public reimplementation that used per-account aggregates instead of per-transaction subgraph patterns scored only 0.152 F1 ([Hugging Face model card](https://huggingface.co/Daemons-Q/qsh-finance-aml-gbm)). The research found **no evidence specific to near-identical amounts, fee-adjusted pass-through exactness, or 7-day windows on HI-Small**.

**Leakage discipline matters more than any single feature.** IBM computes GFP features "in the increasing order of their timestamps" so training rows never see validation or test data ([NeurIPS paper](https://arxiv.org/html/2306.16424v3)). The hecmeynell pipeline deliberately excluded its typology findings from the supervised model because they would leak future information across the split ([hecmeynell repo](https://github.com/hecmeynell-debug/aml-project)). The canonical treatment of leakage defines it as information "that should not be legitimately available to mine from" ([Kaufman, Rosset & Perlich](https://www.cs.umb.edu/~ding/history/470_670_fall_2011/papers/cs670_Tran_PreferredPaper_LeakingInDataMining.pdf)). The 45-task study names threshold selection on the reporting split and random folds on time-indexed data as two of the three flaws that inflate published fraud scores ([45-task study](https://arxiv.org/abs/2608.16147)).

**Class weights shift scores; they do not create ranking power.** XGBoost's own guide says to use `scale_pos_weight` if you care about ranking. If you need correct probabilities, it says not to rebalance and to set `max_delta_step`, for example to 1 ([XGBoost tuning guide](https://xgboost.readthedocs.io/en/stable/tutorials/param_tuning.html); [XGBoost parameters](https://xgboost.readthedocs.io/en/stable/parameter.html)). LightGBM warns that both `is_unbalance` and `scale_pos_weight` "result in poor estimates of the individual class probabilities" ([LightGBM docs](https://lightgbm.readthedocs.io/en/latest/Parameters.html)). Simulation studies find imbalance corrections worsen calibration without improving discrimination, and that shifting the threshold achieves the same sensitivity trade-off ([van den Goorbergh et al.](https://academic.oup.com/jamia/article/29/9/1525/6605096); [Carriero et al.](https://arxiv.org/html/2404.19494v1)). In practice, re-tune the threshold every time you change the class weight, and do not use SMOTE.

PR-AUC is the right threshold-free headline because its random baseline equals the positive rate, here about 0.001. ROC curves are unchanged by class balance and hide the false-alarm count ([Saito & Rehmsmeier](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0118432)).

**Training cost is not the bottleneck.** The research found no s390x benchmark. The nearest published anchor is 1M rows × 50 columns × 500 iterations in about 36 seconds with `hist` on an 8-core desktop CPU ([XGBoost GPU docs](https://xgboost.readthedocs.io/en/release_1.6.0/gpu/index.html)). By my extrapolation, retrains on your roughly 3M training rows take minutes, not hours. Your own timings are the better guide.

## A small GNN costs GPU-hours this VM does not have

**Compute.** Every GNN number above 60 on HI-Small was trained on a GPU. In IBM's own timing on an RTX 3090, a 2-layer, 64-unit GIN took 1,876 seconds. Adding reverse message passing, the adaptation that matters most, pushed it to 12,510 seconds, and full Multi-GIN to **12,958 seconds** ([Multi-GNN, Table 5](https://arxiv.org/abs/2306.11586)). The NeurIPS GIN run took 22,703 seconds on a V100, and the paper's experiments consumed "around 1000 GPU hours" ([NeurIPS paper](https://arxiv.org/html/2306.16424v3)). The only CPU-versus-GPU comparison found is a 3-layer GCN on ogbn-products: 10 seconds per epoch on a 28-core Xeon against 5 on a V100 ([Lin, Deng & Prasanna](https://par.nsf.gov/servlets/purl/10524129)). With 3 cores instead of 28, I extrapolate a full Multi-GNN replication at days, not hours.

**Software.** The PyTorch Geometric CPU wheel index the researcher checked (torch 2.4.0) lists builds of pyg-lib, torch-sparse and torch-scatter for x86-64 Linux, macOS and Windows, and **none for s390x** ([PyG wheel index](https://data.pyg.org/whl/torch-2.4.0+cpu.html)). pyg-lib supplies the graph-sampling routines that mini-batch training relies on, so neighbour-sampled training would need source builds.

**The accuracy case is also weak.** Plain message passing scores 28.7 on HI-Small. It takes reverse message passing (to 46.8), then port numbering (to 56.9), then ego IDs to reach Multi-GIN's 57.2, still below your booster ([Multi-GNN](https://arxiv.org/abs/2306.11586)). A KU Leuven study found GNNs on IBM data underperform simple features. It traced this to "very short average paths and many high-degree nodes", which cause over-smoothing, and reported "highly unstable" training loss ([Deprez et al.](https://arxiv.org/abs/2405.19383)). The one reported gain from feeding GNN embeddings into XGBoost (AUPRC 0.79 to 0.90 on card-fraud data) comes from a vendor page relayed through an AI summary ([NVIDIA blog](https://developer.nvidia.com/blog/optimizing-fraud-detection-in-financial-services-with-graph-neural-networks-and-nvidia-gpus/)). An informal Elliptic++ project saw node2vec embeddings lower XGBoost's PR-AUC from 0.669 to 0.656 ([GraphTabular-FraudFusion](https://github.com/BhaveshBytess/GraphTabular-FraudFusion)).

**If you still want a GNN for the demo, scope it to the candidate graph.** Motif-guided extraction on HI-Small produced a 296-node subgraph that was 22.6% illicit, against 2.4% for random seeds ([feed-guided shortest paths](https://arxiv.org/html/2610.13280)). That shows candidate subgraphs concentrate positives, though no classifier was trained on them. A defensible configuration, my design informed by the cited settings, has these parts:

- **Graph:** your rings plus their one-hop neighbours.
- **Model:** a 2-layer GIN with separate in- and out-aggregation (reverse message passing), trained full-batch in plain PyTorch to avoid the missing compiled libraries.
- **Inputs:** your 32 features plus the XGBoost score as edge attributes.
- **Settings:** IBM's tuned values of learning rate about 0.006, hidden size about 64–66 and minority-class loss weight about 6.3 ([Multi-GNN model_settings.json](https://raw.githubusercontent.com/IBM/Multi-GNN/main/model_settings.json)).

Keep it only if it beats XGBoost on validation PR-AUC.

## Alternatives mostly lose on HI-Small or need hardware you lack

| Alternative | Best evidence on HI-Small (or nearest) | Feasible in hours on 3 cores? | Verdict |
|---|---|---|---|
| IBM Snap ML GFP + XGBoost | 63.23–64.77 F1; snapml 1.17.2 ships s390x wheels for Python 3.9–3.12 under a proprietary licence ([PyPI](https://pypi.org/project/snapml/)) | Install yes; full 5M-edge run unmeasured, roughly 2–7 h by my linear extrapolation from 30–148 ms per batch on 64 threads, possibly less after v1.16.2's faster batch preprocessing | Time-box: test on one day of data first |
| Snap ML zDNN inference on Telum | Accelerates tree-ensemble inference on z16+ and falls back to CPU; imports XGBoost/LightGBM models ([IBM Z Accelerated for Snap ML](https://github.com/IBM/ibmz-accelerated-for-snapml)) | Yes | Good demo garnish for the live scorer; no accuracy change |
| ExSTraQt | 78.90 F1 on GFP splits, recall 70.2, on a 12-core laptop CPU; code at github.com/mhaseebtariq/exstraqt; April 2026 preprint ([ExSTraQt](https://arxiv.org/html/2604.02899v1)) | Unknown: method and runtime not captured | Strongest CPU number found; read the repo only if hours remain |
| MEGA, FraudGT, SALT, Multi-PNA | 64.6–75.8 F1 ([MEGA](https://arxiv.org/html/2412.00241v3)) | No: GPU-trained, 2–92 s per epoch even on an RTX 4090 | Cite as context, don't build |
| BlazingAML-style compiled pattern mining | 46.6 F1 with XGBoost, but 210× faster scatter-gather mining than GFP on CPU ([BlazingAML](https://arxiv.org/html/2604.12241v1)) | No public code found | Skip |
| Isolation Forest / autoencoders | On an HI-Small subset with 0.27% positives, Isolation Forest AUC-PR 0.005–0.024; on Elliptic, AUC-ROC below 0.5 ([Deprez et al.](https://arxiv.org/abs/2405.19383)) | Yes | Skip as a detector |
| Dense-flow and smurfing scores (FlowScope, GARG-AML) | Account-level on HI-Small: FlowScope AUC-ROC 50.0 (chance), GARG-AML 61.2 ([GARG-AML](https://arxiv.org/html/2506.04292)) | Yes | Skip |
| Louvain/Leiden communities | Rabobank study reported no accuracy metrics; ReDiRect reached context-weighted recall 0.713 on IBM HI data within "a couple of hours" on a laptop ([Bonato et al.](https://arxiv.org/html/2409.00823v1); [ReDiRect](https://arxiv.org/html/2604.01315v1)) | Yes | Keep for ring grouping and presentation, not detection |
| Label propagation / "guilt by association" | +13.4 recall points at a real bank; about +5 points on Bitcoin ([Naser Eddin et al.](https://arxiv.org/abs/2112.07508); [GuiltyWalker](https://arxiv.org/abs/2102.05373)) | Yes | Do it, as Move 4 |
| Rule typologies + ML triage | Triage cut false positives 80% while keeping over 90% of true positives, where rules produce over 97% false positives ([Naser Eddin et al.](https://arxiv.org/abs/2112.07508)) | Yes | Use rule tags to explain rings; expect no F1 gain |
| Sequence models (LSTM, transformers) | No HI-Small evidence; a transformer beat XGBoost only on real-bank account histories ([Guéneau et al.](https://arxiv.org/abs/2507.08835)); IBM's fraud demo scores 7-transaction LSTM sequences ([IBM Community](https://community.ibm.com/community/user/blogs/gwendolyn-nguyen/2025/10/24/fraud-detection-on-ibm-i-with-ai-inference-in-a-li)) | No | Skip |
| LLM classification | Unweighted F1 40.7–50.6% on a 1:2 subset against 68.1% for the ML reference; reweighted to real prevalence, about 0.25% ([AMLworld-Compact](https://arxiv.org/html/2610.13627)) | Yes, but useless | Use only for case write-ups |
| Subgraph-level classifiers (Elliptic2) | GLASS test PR-AUC 0.208 on Bitcoin subgraphs; needs candidate subgraphs ([Elliptic2](https://arxiv.org/html/2404.19109v2)) | No | Use ring-level evaluation instead |

The pattern across this table is that methods stripped of supervised boosting lose badly on HI-Small. On the IBM data, unsupervised and flow-based detectors sit near chance. The supervised graph models that win need GPUs.

The IBM-native angle worth showing judges is cheap. IBM positions GFP plus a booster as its own production pattern; the GFP paper describes it as bundled with Cloud Pak for Data on Z and the AI Toolkit for IBM Z and LinuxONE ([GFP paper](https://arxiv.org/html/2402.08593v2)). Snap ML can serve an imported XGBoost model on the Telum accelerator where present. Release notes from October 2025 confirm automatic fallback to CPU "when zdnn rpm are missing" ([Snap ML release notes](https://snapml.readthedocs.io/en/latest/release_notes.html)). On s390x, the install guide recommends creating a conda environment with numpy, scipy and scikit-learn first, because those dependencies lack s390x wheels and compile slowly from PyPI ([Snap ML installation](https://snapml.readthedocs.io/en/latest/installation.html)).

A sobering external check: one 2026 study re-ran state-of-the-art detectors in a realistic bank-then-regulator pipeline. Recall fell from 74% to 24%, and FraudGT's to 7% ([Coscia blog on the Royal Society Open Science study](https://www.michelecoscia.com/?p=2532)). Benchmark F1 on HI-Small measures pattern recognition in a simulator, not deployment readiness.

## What the five transcripts could not settle

**Hardware and runtime.**

- No source benchmarked XGBoost, LightGBM, GFP or any GNN on s390x, on LinuxONE, or on three cores. Every runtime above for your hardware is my extrapolation.
- GFP's runtime against thread count is published only as charts, with no numbers.
- Whether your VM sits on a Telum-equipped z16 or later, which inference acceleration requires, is unknown.
- PyG on s390x was checked against only one wheel index; building it from source was not tested.

**Methods with no HI-Small evidence.**

- No study tests second-pass score features, collective classification or stacking on HI-Small. The evidence for Move 4 comes from a real bank and from Bitcoin.
- No source quantified near-identical-amount counts, fee-adjusted pass-through exactness, 7-day windows, focal loss against `scale_pos_weight`, or seed-averaging gains on AML data.
- ExSTraQt's method, runtime and independent replication were not captured beyond its results table, and its top numbers are self-reported in a preprint.

**Unreachable sources.**

- Fetches for MonLAD (streaming laundering-agent detection), IBM's TabFormer paper, LAS-GNN and the FATF typology report were denied or blocked, so those are absent.
- The Kaggle maintainers' exact statement about post-period transactions came only through a search snippet.

**Gaps in published baselines.**

- No paper reports ring- or attempt-level detection metrics for HI-Small beyond Multi-GNN's per-pattern recall. Your ring-level evaluation against the patterns file has no external baseline to compare against.
- The dataset paper's own HI-Small statistics are internally inconsistent: 3.6K laundering transactions against a rate implying about 5.2K.

## Conclusion

The research reframes the problem. On HI-Small, model class has stopped being the constraint for a team already at 0.63. What remains is coverage of the laundering that sits outside the eight typologies, and the honesty of the yardstick. The adapted GNNs that beat boosting win by propagating evidence along money flows. Your ring service already does that after the fact, so the most promising untested idea is to make that propagation a training-time input with cross-fitted scores. It is cheap, it runs on three cores, and its mechanism matches how AMLworld labels the data.

The 20-to-81 spread in published HI-Small scores is substantially a story of splits, thresholds, seeds, GPUs and a simulator tail where legitimate traffic disappears. A result framed as "matches IBM's GBDT+GFP baseline under the identical causal protocol, with per-period and per-pattern breakdowns and a validation-frozen threshold" will survive scrutiny that a bigger, less comparable number would not.
