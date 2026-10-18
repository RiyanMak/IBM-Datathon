"""A small graph neural network that gives a ring a second opinion (step 4, part B).

Accounts are nodes, transfers are directed edges carrying the 32 clues, the XGBoost
score and the amount. Two message-passing layers: every account gathers messages
from its incoming transfers and, separately, from its outgoing ones (direction
matters: receiving and sending mean different things), then updates its own vector.
An edge classifier reads [sender vector, receiver vector, edge features] and outputs
a laundering logit. Plain PyTorch, no graph library: the sum over arrows is index_add_.

It is trained offline on the candidate subgraph: transfers XGBoost scores at or above
the ring service's expand threshold, which is exactly what the ring service holds in
its window (scripts/train_gnn.py). Live, the ring service builds the same kind of
subgraph around a ring and averages the GNN's edge probabilities over the ring's
transfers.

Weights are saved as a NumPy .npz, not a pickle: NumPy records byte order, so a file
written on a laptop loads correctly on the big-endian LinuxONE machine.
"""

import json
from pathlib import Path

import numpy as np

NODE_FEATURES = ("log_in_count", "log_out_count", "log_in_usd", "log_out_usd", "mean_in_score", "mean_out_score")
WEIGHTS_NAME = "gnn.npz"
META_NAME = "gnn_meta.json"


def build_graph(src, dst, clues, scores, amounts):
    """Index the accounts and compute node features from the edges.

    src, dst: account ids per edge; clues: (E, F) clue matrix; scores: (E,) XGBoost
    scores; amounts: (E,) USD. Node features are per-account totals over these edges.
    Returns a dict of numpy arrays: src_idx, dst_idx, x_edge (E, F+2), x_node (N, 6), accounts.
    """
    index = {}
    src_idx = np.fromiter((index.setdefault(a, len(index)) for a in src), dtype=np.int64, count=len(src))
    dst_idx = np.fromiter((index.setdefault(b, len(index)) for b in dst), dtype=np.int64, count=len(dst))
    n = len(index)
    scores = np.asarray(scores, dtype=np.float64)
    amounts = np.asarray(amounts, dtype=np.float64)
    in_count = np.bincount(dst_idx, minlength=n).astype(np.float64)
    out_count = np.bincount(src_idx, minlength=n).astype(np.float64)
    in_usd = np.bincount(dst_idx, weights=amounts, minlength=n)
    out_usd = np.bincount(src_idx, weights=amounts, minlength=n)
    in_score = np.bincount(dst_idx, weights=scores, minlength=n) / np.maximum(in_count, 1)
    out_score = np.bincount(src_idx, weights=scores, minlength=n) / np.maximum(out_count, 1)
    x_node = np.stack([np.log1p(in_count), np.log1p(out_count), np.log1p(np.maximum(in_usd, 0)),
                       np.log1p(np.maximum(out_usd, 0)), in_score, out_score], axis=1).astype(np.float32)
    x_edge = np.concatenate([np.asarray(clues, dtype=np.float32), scores[:, None].astype(np.float32),
                             np.log1p(np.maximum(amounts, 0))[:, None].astype(np.float32)], axis=1)
    accounts = [None] * n
    for a, i in index.items():
        accounts[i] = a
    return {"src_idx": src_idx, "dst_idx": dst_idx, "x_edge": x_edge, "x_node": x_node, "accounts": accounts}


def _torch():
    import torch
    return torch


def make_model(edge_dim, node_dim=len(NODE_FEATURES), hidden=32, layers=2):
    """The network. Defined inside a function so this module imports without torch."""
    torch = _torch()
    nn = torch.nn

    class RingGNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.edge_dim, self.node_dim, self.hidden, self.n_layers = edge_dim, node_dim, hidden, layers
            self.register_buffer("edge_mean", torch.zeros(edge_dim))
            self.register_buffer("edge_std", torch.ones(edge_dim))
            self.register_buffer("node_mean", torch.zeros(node_dim))
            self.register_buffer("node_std", torch.ones(node_dim))
            self.edge_in = nn.Linear(edge_dim, hidden)
            self.node_in = nn.Linear(node_dim, hidden)
            self.layers = nn.ModuleList([nn.ModuleDict({
                "msg_in": nn.Linear(2 * hidden, hidden),    # what an incoming transfer tells the receiver
                "msg_out": nn.Linear(2 * hidden, hidden),   # what an outgoing transfer tells the sender
                "mix": nn.Linear(3 * hidden, hidden),       # the account's update from both
            }) for _ in range(layers)])
            self.classifier = nn.Sequential(nn.Linear(3 * hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))

        def set_normalisation(self, x_edge, x_node):
            """Standardise inputs with the training graph's statistics (stored in the model)."""
            x_edge, x_node = torch.as_tensor(x_edge), torch.as_tensor(x_node)
            self.edge_mean.copy_(x_edge.mean(0))
            self.edge_std.copy_(x_edge.std(0).clamp(min=1e-6))
            self.node_mean.copy_(x_node.mean(0))
            self.node_std.copy_(x_node.std(0).clamp(min=1e-6))

        def forward(self, x_node, x_edge, src, dst):
            n = x_node.size(0)
            e = torch.relu(self.edge_in((x_edge - self.edge_mean) / self.edge_std))
            h = torch.relu(self.node_in((x_node - self.node_mean) / self.node_std))
            deg_in = torch.bincount(dst, minlength=n).clamp(min=1).unsqueeze(1).to(h.dtype)
            deg_out = torch.bincount(src, minlength=n).clamp(min=1).unsqueeze(1).to(h.dtype)
            for layer in self.layers:
                m_in = torch.relu(layer["msg_in"](torch.cat([h[src], e], 1)))
                m_out = torch.relu(layer["msg_out"](torch.cat([h[dst], e], 1)))
                agg_in = torch.zeros(n, self.hidden, dtype=h.dtype).index_add_(0, dst, m_in) / deg_in
                agg_out = torch.zeros(n, self.hidden, dtype=h.dtype).index_add_(0, src, m_out) / deg_out
                h = h + torch.relu(layer["mix"](torch.cat([h, agg_in, agg_out], 1)))   # 2 layers = 2-hop context
            return self.classifier(torch.cat([h[src], h[dst], e], 1)).squeeze(1)

    return RingGNN()


def save_model(model, meta: dict, models_dir):
    """Weights as .npz (byte order recorded, loads on s390x) plus a JSON with the shapes and thresholds."""
    models_dir = Path(models_dir)
    arrays = {k: v.detach().cpu().numpy() for k, v in model.state_dict().items()}
    np.savez(models_dir / WEIGHTS_NAME, **arrays)
    meta = dict(meta, edge_dim=model.edge_dim, node_dim=model.node_dim, hidden=model.hidden, layers=model.n_layers)
    (models_dir / META_NAME).write_text(json.dumps(meta, indent=2))


def load_model(models_dir):
    """Returns (model in eval mode, meta) or (None, None) if no GNN has been trained."""
    models_dir = Path(models_dir)
    if not (models_dir / WEIGHTS_NAME).exists() or not (models_dir / META_NAME).exists():
        return None, None
    torch = _torch()
    meta = json.loads((models_dir / META_NAME).read_text())
    model = make_model(meta["edge_dim"], meta["node_dim"], meta["hidden"], meta["layers"])
    with np.load(models_dir / WEIGHTS_NAME) as arrays:
        state = {k: torch.from_numpy(np.ascontiguousarray(arrays[k]).astype(np.float32)) for k in arrays.files}
    model.load_state_dict(state)
    model.eval()
    return model, meta


def predict(model, graph) -> np.ndarray:
    """Laundering probability per edge of a graph from build_graph."""
    torch = _torch()
    with torch.no_grad():
        logits = model(torch.as_tensor(graph["x_node"]), torch.as_tensor(graph["x_edge"]),
                       torch.as_tensor(graph["src_idx"]), torch.as_tensor(graph["dst_idx"]))
        return torch.sigmoid(logits).numpy()
