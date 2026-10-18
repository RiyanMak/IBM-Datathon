"""Presentation logic for the investigation workspace; no UI or Redis dependencies."""
import hashlib
import json
import math
from collections import defaultdict

from services.page import fmt, graph

MAX_NODES = 36
MAX_TRANSFERS = 60
PAGE_SIZE = 12


def accounts(ring):
    return list(dict.fromkeys(ring.accounts + [a for e in ring.edges for a in (e.src, e.dst)]))


def transfers(ring):
    """Stable selection keys, even for missing/duplicate transaction identifiers."""
    occurrences = defaultdict(int)
    rows = []
    for edge in ring.edges:
        identity = json.dumps([edge.txn_id, edge.src, edge.dst, edge.ts], ensure_ascii=False)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
        occurrences[digest] += 1
        rows.append(("transfer:%s:%d" % (digest, occurrences[digest]), edge))
    return sorted(rows, key=lambda row: (row[1].ts, row[0]))


def options(ring):
    return ["overview"] + ["account:" + a for a in accounts(ring)] + [k for k, _ in transfers(ring)]


def selection_label(ring, key):
    if key == "overview":
        return "Whole case"
    if key.startswith("account:"):
        return "Account · " + key[len("account:"):]
    edge = dict(transfers(ring)).get(key)
    if edge:
        return "Transfer %s · %s → %s · %s" % (
            edge.txn_id or "without ID", edge.src, edge.dst, fmt.data_time(edge.ts))
    return "Whole case"


def selected_edge(ring, key):
    return dict(transfers(ring)).get(key)


def related_transfers(ring, key):
    rows = transfers(ring)
    if key.startswith("account:"):
        account = key[len("account:"):]
        return [(k, e) for k, e in rows if account in (e.src, e.dst)]
    if key.startswith("transfer:"):
        return [(k, e) for k, e in rows if k == key]
    return rows


def volume(edges):
    known = [e.amount_usd for e in edges if e.amount_usd is not None]
    if not known:
        return fmt.DASH
    return "≈ " + graph.money(sum(known)) + (" · partial" if len(known) != len(edges) else "")


def native_amount(amount, currency):
    if amount is None or not currency:
        return "Not supplied"
    return "{:,.2f} {}".format(amount, currency)


def flow_description(ring):
    """Describe only the observed connections, without asserting laundering."""
    edges = [e for e in ring.edges if e.src != e.dst]
    if not edges:
        return ("Only self-transfers are present. These do not establish a multi-account ring."
                if ring.edges else "No transfers have been supplied for this case.")
    outgoing, incoming = defaultdict(set), defaultdict(set)
    for e in edges:
        outgoing[e.src].add(e.dst)
        incoming[e.dst].add(e.src)
    spreads = any(len(v) > 1 for v in outgoing.values())
    converges = any(len(v) > 1 for v in incoming.values())
    if spreads and converges:
        return "Transfers spread across several accounts and converge elsewhere in this case."
    if spreads:
        return "One or more accounts send funds to several recipients. Follow an arrow to inspect a transfer."
    if converges:
        return "Transfers from several accounts converge on a recipient. Inspect its incoming transfers."
    return "Follow the arrows from sender to recipient, then compare the transfer times below."


def provenance(case):
    if not case:
        return "Summary pending"
    if case.check_note.upper().startswith("FAKE"):
        return "Simulated upgrade · template text"
    if case.fact_checked:
        return "Granite · fact check passed"
    return case.source_label


def stable_queue(previous, ranked, apply=False):
    """Hold existing rows in place until the user explicitly applies new ranking."""
    if previous is None or apply:
        return list(ranked), 0
    visible = [rid for rid in previous if rid in ranked]
    return visible, len(set(ranked) - set(previous))


def graph_data(ring, selection="overview", positions=None):
    """Bounded deterministic graph. Canonical IDs remain the identity throughout.

    Preserve existing positions across versions. A fresh layout is available via
    the explicit fit/reset control. Transfer details never depend on graph scope.
    """
    all_accounts = accounts(ring)
    all_rows = transfers(ring)
    selected = selected_edge(ring, selection)
    focus = selection[len("account:"):] if selection.startswith("account:") else None
    ordered = sorted(all_rows, key=lambda row: (
        row[0] == selection or (focus is not None and focus in (row[1].src, row[1].dst)),
        row[1].amount_usd is not None, row[1].amount_usd or 0), reverse=True)
    nodes = []
    for a in ([focus] if focus else ([selected.src, selected.dst] if selected else [])):
        if a not in nodes:
            nodes.append(a)
    chosen = []
    for key, edge in ordered:
        additions = [a for a in (edge.src, edge.dst) if a not in nodes]
        if len(chosen) < MAX_TRANSFERS and len(nodes) + len(set(additions)) <= MAX_NODES:
            nodes.extend(a for a in additions if a not in nodes)
            chosen.append((key, edge))
    for a in all_accounts:
        if len(nodes) < MAX_NODES and a not in nodes:
            nodes.append(a)
    nodes = [a for a in all_accounts if a in nodes]
    pos = layout(nodes, [e for _, e in chosen])
    # Keep all positions (including currently hidden nodes) for incremental updates.
    saved = dict(positions or {})
    occupied = set(tuple(p) for p in saved.values())
    for a in nodes:
        if a not in saved:
            x, y = pos[a]
            while any(abs(x - ox) < 90 and abs(y - oy) < 68 for ox, oy in occupied):
                y += 76
            saved[a] = [x, y]
            occupied.add((x, y))
    links = [{"key": k, "src": e.src, "dst": e.dst,
              "label": "%s → %s · %s estimated USD · %s" % (
                  e.src, e.dst, graph.money(e.amount_usd), fmt.data_time(e.ts))}
             for k, e in chosen]
    height = max([360] + [saved[a][1] + 64 for a in nodes])
    node_roles = roles(nodes, [e for _, e in chosen])
    return {
        "nodes": [{"id": a, "label": "A%02d" % (all_accounts.index(a) + 1),
                   "short": a if len(a) <= 15 else a[:6] + "…" + a[-6:],
                   "role": node_roles.get(a, ""),
                   "x": saved[a][0], "y": saved[a][1]} for a in nodes],
        "edges": links, "selection": selection, "width": 960, "height": height,
        "positions": saved,
        "scope": "%d of %d accounts · %d of %d transfers" % (
            len(nodes), len(all_accounts), len(chosen), len(all_rows)),
        "limited": len(nodes) < len(all_accounts) or len(chosen) < len(all_rows),
    }


def roles(nodes, edges):
    """Inferred flow roles from the drawn transfers: where money enters and where it ends.

    An origin sends but never receives within the case. A final recipient receives but
    never sends on. A pure cycle has neither, so the earliest sender becomes the origin.
    Self-transfers never define a role. These are inferences, not dataset facts.
    """
    flows = [e for e in edges if e.src != e.dst and e.src in nodes and e.dst in nodes]
    senders = {e.src for e in flows}
    receivers = {e.dst for e in flows}
    found = {a: "origin" for a in nodes if a in senders and a not in receivers}
    found.update({a: "endpoint" for a in nodes if a in receivers and a not in senders})
    if flows and not any(role == "origin" for role in found.values()):
        found[min(flows, key=lambda e: (e.ts, e.src)).src] = "origin"
    return found


def layout(nodes, edges):
    """Layer acyclic flows. Place cyclic/disconnected remainders in a circle."""
    if not nodes:
        return {}
    outgoing = {a: set() for a in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for edge in edges:
        if edge.src != edge.dst and edge.dst not in outgoing[edge.src]:
            outgoing[edge.src].add(edge.dst)
            indegree[edge.dst] += 1
    ready = [a for a in nodes if indegree[a] == 0]
    rank = dict.fromkeys(ready, 0)
    visited = []
    while ready:
        a = ready.pop(0)
        visited.append(a)
        for b in sorted(outgoing[a]):
            rank[b] = max(rank.get(b, 0), rank[a] + 1)
            indegree[b] -= 1
            if indegree[b] == 0:
                ready.append(b)
    if len(visited) != len(nodes):
        # Circles expose return paths, and do not pretend disconnected nodes connect.
        radius_y = max(110, len(nodes) * 15)
        return {a: [480 + 320 * math.cos(-math.pi / 2 + i * 2 * math.pi / len(nodes)),
                    radius_y + 105 + radius_y * math.sin(-math.pi / 2 + i * 2 * math.pi / len(nodes))]
                for i, a in enumerate(nodes)}
    levels = defaultdict(list)
    for a in nodes:
        levels[rank[a]].append(a)
    max_rank = max(levels)
    height = max(290, max(len(v) for v in levels.values()) * 76)
    return {a: [90 + level * 780 / max(1, max_rank) if max_rank else 480,
                (i + .5) * height / len(group) + 30]
            for level, group in levels.items() for i, a in enumerate(group)}
