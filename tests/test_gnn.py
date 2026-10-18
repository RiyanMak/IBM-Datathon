"""Tests for common/gnn.py and scripts/train_gnn.py (skipped without torch)."""
import json

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from common.features import FEATURE_NAMES
from common.gnn import NODE_FEATURES, build_graph, load_model, make_model, predict, save_model
from scripts.train_gnn import best_f1_threshold, train

F = len(FEATURE_NAMES)


def toy_graph(n_edges=300, seed=0):
    """Random transfers among 40 accounts; laundering when the receiver is a 'collector'."""
    rng = np.random.default_rng(seed)
    accounts = [f"{b}_{i:03X}" for b in (10, 27) for i in range(20)]
    collectors = set(accounts[:4])
    src = rng.choice(accounts, n_edges)
    dst = rng.choice(accounts, n_edges)
    keep = src != dst
    src, dst = src[keep], dst[keep]
    clues = rng.normal(size=(len(src), F)).astype(np.float32)
    y = np.array([1.0 if d in collectors else 0.0 for d in dst], dtype=np.float32)
    clues[:, 0] += 2 * y                              # one clue carries signal
    scores = np.clip(0.5 + 0.3 * y + rng.normal(0, 0.1, len(src)), 0, 1)
    amounts = rng.lognormal(6, 1, len(src))
    g = build_graph(src, dst, clues, scores, amounts)
    g["y"] = y
    g["xgb"] = scores.astype(np.float32)
    return g


def test_build_graph_indexes_accounts_and_node_features():
    g = build_graph(["a", "b", "a"], ["b", "c", "c"], np.zeros((3, F), np.float32), [0.9, 0.8, 0.7], [100.0, 50.0, 25.0])
    assert g["accounts"] == ["a", "b", "c"]
    assert g["src_idx"].tolist() == [0, 1, 0] and g["dst_idx"].tolist() == [1, 2, 2]
    assert g["x_edge"].shape == (3, F + 2) and g["x_node"].shape == (3, len(NODE_FEATURES))
    node_a = dict(zip(NODE_FEATURES, g["x_node"][0]))
    assert node_a["log_out_count"] == pytest.approx(np.log1p(2)) and node_a["log_in_count"] == 0
    assert node_a["log_out_usd"] == pytest.approx(np.log1p(125.0)) and node_a["mean_out_score"] == pytest.approx(0.8)
    assert g["x_edge"][0, F] == pytest.approx(0.9) and g["x_edge"][0, F + 1] == pytest.approx(np.log1p(100.0))


def test_forward_shapes_and_direction_matters():
    g = toy_graph()
    model = make_model(g["x_edge"].shape[1])
    model.set_normalisation(g["x_edge"], g["x_node"])
    p = predict(model, g)
    assert p.shape == (len(g["y"]),) and np.all((p >= 0) & (p <= 1))
    # reversing every arrow changes the prediction: the model sees direction
    rev = dict(g, src_idx=g["dst_idx"], dst_idx=g["src_idx"])
    assert not np.allclose(predict(model, rev), p)


def test_training_learns_the_toy_signal_and_round_trips_through_npz(tmp_path):
    graphs = {"fit": toy_graph(600, seed=1), "stop": toy_graph(300, seed=2)}
    model, val_ap = train(graphs, hidden=16, layers=2, epochs=60, lr=0.02, patience=60, seed=0)
    base_rate = graphs["stop"]["y"].mean()
    assert val_ap > 2 * base_rate                       # far better than chance on the toy signal
    test = toy_graph(300, seed=3)
    p = predict(model, test)
    thr = best_f1_threshold(graphs["stop"]["y"], predict(model, graphs["stop"]))
    assert 0 < thr < 1

    save_model(model, {"gnn_threshold": thr, "expand_threshold": 0.5}, tmp_path)
    assert (tmp_path / "gnn.npz").exists() and (tmp_path / "gnn_meta.json").exists()
    meta = json.loads((tmp_path / "gnn_meta.json").read_text())
    assert meta["edge_dim"] == F + 2 and meta["hidden"] == 16 and meta["layers"] == 2
    loaded, meta2 = load_model(tmp_path)
    assert np.allclose(predict(loaded, test), p, atol=1e-6)
    assert meta2["gnn_threshold"] == thr


def split_rows():
    """Transfers over 10 days: days 0-5 train, 6-7 validation, 8-9 test; account A pays B once a day."""
    import pandas as pd
    rows = []
    for day in range(10):
        split = "train" if day < 6 else "validation" if day < 8 else "test"
        for k, (src, dst, y) in enumerate([("A", "B", 1), ("A", "C", 0), ("C", "D", 0)]):
            rows.append({"txn_id": 10 * day + k, "split": split, "label": y, "amount_usd": 100.0,
                         "ts_min": day * 1440 + 60 * k, "from_acct": src, "to_acct": dst,
                         **{name: 0.0 for name in FEATURE_NAMES}})
    return pd.DataFrame(rows)


def test_graphs_fit_on_validation_and_cut_into_24_hour_blocks():
    from scripts.train_gnn import make_graphs
    rows = split_rows()
    scores = np.full(len(rows), 0.9)
    scores[rows.from_acct.eq("C").to_numpy()] = 0.1          # below expand: not a candidate
    g = make_graphs(rows, scores, expand=0.5, fit_split="validation", stop_share=0.25)
    # validation is days 6 and 7: the first 75 % of its time span is fitted, the rest held out
    assert sorted(g["fit"]["txn_id"].tolist()) == [60, 61] and sorted(g["stop"]["txn_id"].tolist()) == [70, 71]
    assert sorted(g["test"]["txn_id"].tolist()) == [80, 81, 90, 91]
    # one node per account per day: A on day 8 and A on day 9 are different nodes
    assert sorted(g["test"]["accounts"]) == ["8|A", "8|B", "8|C", "9|A", "9|B", "9|C"]
    node_a = dict(zip(NODE_FEATURES, g["test"]["x_node"][g["test"]["accounts"].index("8|A")]))
    assert node_a["log_out_count"] == pytest.approx(np.log1p(2))   # its day's two transfers, not four

    g = make_graphs(rows, scores, expand=0.5, fit_split="train")
    assert len(g["fit"]["y"]) == 12 and len(g["stop"]["y"]) == 4


def test_load_model_without_files_returns_none(tmp_path):
    assert load_model(tmp_path) == (None, None)
