"""The GNN itself, in plain PyTorch. No PyTorch Geometric needed.

How it works, in one paragraph: every account (node) starts with a few
numbers about itself (how much came in, how much went out, ...). Every
transfer (edge) also has a few numbers (amount, time, format, tier-1
score). A "message passing" layer lets each account look at the transfers
touching it and the accounts on the other end, and update its own numbers.
We do that twice, so each account "sees" two hops around itself. Finally a
small classifier looks at a transfer plus its two updated accounts and says
how likely that transfer is laundering. The ring score is the average of
the strongest few transfer scores inside the ring.
"""
import json
from typing import Dict

import torch
import torch.nn as nn
import torch.nn.functional as F

from services.gnn.graphdata import EDGE_FEATURES, NODE_FEATURES, SubGraph


class MessagePassingLayer(nn.Module):
    """One round of: build a message on each edge, add messages up at each
    node (in both directions), update the node."""

    def __init__(self, d: int):
        super().__init__()
        self.msg = nn.Linear(3 * d, d)
        self.upd = nn.Linear(3 * d, d)
        self.norm = nn.LayerNorm(d)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        src, dst = edge_index[0], edge_index[1]
        n = x.size(0)
        m = F.relu(self.msg(torch.cat([x[src], x[dst], e], dim=1)))   # [E, d]

        # Money flows src -> dst, so dst "receives" the message (forward)
        # and src also gets to hear about it (reverse). index_add_ is the
        # whole trick: it sums rows of m into the right node slots.
        agg_fwd = torch.zeros(n, m.size(1), dtype=m.dtype).index_add_(0, dst, m)
        agg_rev = torch.zeros(n, m.size(1), dtype=m.dtype).index_add_(0, src, m)
        deg_in = torch.bincount(dst, minlength=n).clamp(min=1).unsqueeze(1).to(m.dtype)
        deg_out = torch.bincount(src, minlength=n).clamp(min=1).unsqueeze(1).to(m.dtype)

        h = self.upd(torch.cat([x, agg_fwd / deg_in, agg_rev / deg_out], dim=1))
        return self.norm(x + F.relu(h))                                 # residual


class RingGNN(nn.Module):
    def __init__(self, d: int = 32, layers: int = 2):
        super().__init__()
        self.d = d
        self.node_enc = nn.Linear(len(NODE_FEATURES), d)
        self.edge_enc = nn.Linear(len(EDGE_FEATURES), d)
        self.layers = nn.ModuleList([MessagePassingLayer(d) for _ in range(layers)])
        self.head = nn.Sequential(nn.Linear(3 * d, d), nn.ReLU(), nn.Linear(d, 1))

    def forward(self, g: SubGraph) -> torch.Tensor:
        """Return one logit per edge (higher = more likely laundering)."""
        x = F.relu(self.node_enc(g.x))
        e = F.relu(self.edge_enc(g.edge_attr))
        for layer in self.layers:
            x = layer(x, g.edge_index, e)
        src, dst = g.edge_index[0], g.edge_index[1]
        return self.head(torch.cat([x[src], x[dst], e], dim=1)).squeeze(1)

    @torch.no_grad()
    def edge_probs(self, g: SubGraph) -> torch.Tensor:
        self.eval()
        return torch.sigmoid(self.forward(g))


def ring_score_from_probs(probs: torch.Tensor, k: int = 5) -> float:
    """Average of the top-k edge probabilities. One lucky edge cannot carry a
    ring, and a few boring edges cannot sink one."""
    if probs.numel() == 0:
        return 0.0
    k = min(k, probs.numel())
    return float(probs.topk(k).values.mean())


# ---- Portable weights -----------------------------------------------------
# Saved as JSON (plain lists of numbers) so the same file loads on a laptop
# (little-endian x86/arm) and on LinuxONE (big-endian s390x) with no pickle
# or byte-order worries. The team guide says "never pickle"; this is why.

def save_weights_json(model: RingGNN, path: str, extra: Dict = None) -> None:
    payload = {
        "arch": {"d": model.d, "layers": len(model.layers)},
        "features": {"edge": EDGE_FEATURES, "node": NODE_FEATURES},
        "state": {k: {"shape": list(v.shape), "data": v.detach().cpu().flatten().tolist()}
                  for k, v in model.state_dict().items()},
    }
    if extra:
        payload["extra"] = extra
    with open(path, "w") as f:
        json.dump(payload, f)


def load_weights_json(path: str) -> RingGNN:
    with open(path) as f:
        payload = json.load(f)
    if payload["features"]["edge"] != EDGE_FEATURES or payload["features"]["node"] != NODE_FEATURES:
        raise ValueError("Weights were trained with a different feature list. Retrain.")
    model = RingGNN(d=payload["arch"]["d"], layers=payload["arch"]["layers"])
    state = {k: torch.tensor(v["data"], dtype=torch.float32).reshape(v["shape"])
             for k, v in payload["state"].items()}
    model.load_state_dict(state)
    model.eval()
    return model
