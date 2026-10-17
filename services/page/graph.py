"""A ring as Graphviz DOT text. No Streamlit: st.graphviz_chart draws it, and the
Flask fallback can hand the same text to viz.js.

    pic = graph.build(ring, facts=case.facts if case else None, theme="light")
    st.graphviz_chart(pic.dot)

Accounts are boxes, transfers are arrows labelled with dollar amounts. Parallel
transfers between the same two accounts merge into one arrow ("$3,200 x2"). The
key account, origin and collector come from the case writer's facts pack; with
no facts nothing is highlighted, and the picture still draws. Each role is
written inside its box as well as coloured, so colour never carries it alone.

Colours are the dataviz reference palette: the first three categorical slots
(validated as a set for colour-blind readers), neutral ink for everything else.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from services.page.models import RingView

MAX_EDGES = 60   # bigger rings keep their largest transfers; the caption says so
MAX_SIZE_IN = "10,6.5"   # Graphviz shrinks a bigger picture to fit (inches at 72 px); never enlarges

ROLE_ORDER = ("key_account", "origin", "collector")   # fixed slot order, never cycled
ROLE_TEXT = {
    "key_account": "Key account (possible victim)",
    "origin": "Origin",
    "collector": "Collector",
}

THEMES = {
    "light": {
        "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "node_line": "#c3c2b7", "node_fill": "#fcfcfb", "edge": "#898781",
        "roles": {"key_account": "#2a78d6", "origin": "#eb6834", "collector": "#1baf7a"},
    },
    "dark": {
        "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "node_line": "#52514e", "node_fill": "#1a1a19", "edge": "#898781",
        "roles": {"key_account": "#3987e5", "origin": "#d95926", "collector": "#199e70"},
    },
}
FONT = "Segoe UI, Helvetica, Arial, sans-serif"


@dataclass
class RingPicture:
    dot: str
    shown_edges: int          # arrows' underlying transfers drawn
    total_edges: int
    roles: Dict[str, str] = field(default_factory=dict)   # account -> role key

    @property
    def truncated(self) -> bool:
        return self.shown_edges < self.total_edges

    @property
    def caption(self) -> str:
        if self.truncated:
            return "Showing the %d largest of %d transfers." % (self.shown_edges, self.total_edges)
        return ""


def money(x: Optional[float]) -> str:
    """9800.0 -> '$9,800'; 950.25 -> '$950'; None -> '$?'."""
    if x is None:
        return "$?"
    return "${:,.0f}".format(x)


def esc(text) -> str:
    """Safe inside a double-quoted DOT string."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def roles_from_facts(facts: Optional[dict], accounts) -> Dict[str, str]:
    """account -> role, from the facts pack. Unknown accounts are ignored. If one
    account holds two roles, the earlier role in ROLE_ORDER wins."""
    roles: Dict[str, str] = {}
    if not facts:
        return roles
    known = set(accounts)
    for role in ROLE_ORDER:
        acct = facts.get(role)
        if acct and acct in known and acct not in roles:
            roles[acct] = role
    return roles


def _pen(amount: float, hi: float) -> float:
    """Arrow width 1.0-3.0 px: 3 px for the ring's largest arrow, 1 px at 1/100 of it
    or less (log scale). Similar amounts get similar widths."""
    if hi <= 0 or amount <= 0:
        return 1.0
    t = 1.0 + math.log10(amount / hi) / 2.0
    return round(1.0 + 2.0 * min(max(t, 0.0), 1.0), 2)


def build(ring: RingView, facts: Optional[dict] = None, theme: str = "light",
          max_edges: int = MAX_EDGES) -> RingPicture:
    t = THEMES.get(theme, THEMES["light"])

    edges = list(ring.edges)
    total = len(edges)
    if total > max_edges:
        edges = sorted(edges, key=lambda e: e.amount_usd or 0.0, reverse=True)[:max_edges]

    # Accounts: the ring's list first (keeps the ring service's order), then any
    # account that only appears on an edge.
    accounts: List[str] = []
    for a in ring.accounts + [x for e in edges for x in (e.src, e.dst)]:
        if a not in accounts:
            accounts.append(a)
    if total > max_edges:   # drop accounts no drawn arrow touches
        touched = {x for e in edges for x in (e.src, e.dst)}
        accounts = [a for a in accounts if a in touched]

    roles = roles_from_facts(facts, accounts)

    # Merge parallel transfers.
    merged: Dict[tuple, dict] = {}
    for e in edges:
        m = merged.setdefault((e.src, e.dst), {"amount": 0.0, "n": 0, "rows": [], "known": False})
        m["amount"] += e.amount_usd or 0.0
        m["known"] = m["known"] or e.amount_usd is not None
        m["n"] += 1
        m["rows"].append(e)

    sent: Dict[str, float] = {}
    recv: Dict[str, float] = {}
    for e in edges:
        sent[e.src] = sent.get(e.src, 0.0) + (e.amount_usd or 0.0)
        recv[e.dst] = recv.get(e.dst, 0.0) + (e.amount_usd or 0.0)

    lines = [
        "digraph ring {",
        '  graph [rankdir=LR, bgcolor="transparent", pad="0.2", nodesep="0.35", ranksep="0.9",'
        ' size="%s", fontname="%s"];' % (MAX_SIZE_IN, FONT),
        '  node [shape=box, style="rounded,filled", fontname="%s", fontsize=11, margin="0.15,0.07",'
        ' color="%s", fillcolor="%s", fontcolor="%s", penwidth=1];'
        % (FONT, t["node_line"], t["node_fill"], t["ink"]),
        '  edge [fontname="%s", fontsize=10, color="%s", fontcolor="%s", arrowsize=0.7];'
        % (FONT, t["edge"], t["ink2"]),
    ]

    for a in accounts:
        tip = "%s&#10;received %s&#10;sent %s" % (a, money(recv.get(a, 0.0)), money(sent.get(a, 0.0)))
        role = roles.get(a)
        if role:
            colour = t["roles"][role]
            label = '<<b>%s</b><br/><font point-size="10" color="%s">%s</font>>' % (
                _html(a), t["ink"], _html(ROLE_TEXT[role]))
            lines.append('  "%s" [label=%s, color="%s", fillcolor="%s", penwidth=2.5, tooltip="%s"];'
                         % (esc(a), label, colour, colour + "22", esc(tip)))
        else:
            lines.append('  "%s" [label="%s", tooltip="%s"];' % (esc(a), esc(a), esc(tip)))

    amounts = [m["amount"] for m in merged.values() if m["amount"] > 0]
    hi = max(amounts) if amounts else 0.0
    for (src, dst), m in merged.items():
        label = (money(m["amount"]) if m["known"] else money(None)) + (" x%d" % m["n"] if m["n"] > 1 else "")
        tip_rows = []
        for e in sorted(m["rows"], key=lambda x: x.ts):
            score = "" if e.score is None else ", score %.2f" % e.score
            tip_rows.append("%s %s%s" % (e.ts.replace("T", " ")[:16], money(e.amount_usd), score))
        tip = "%s -> %s&#10;%s" % (src, dst, "&#10;".join(tip_rows))
        lines.append('  "%s" -> "%s" [label="%s", penwidth=%s, tooltip="%s", labeltooltip="%s"];'
                     % (esc(src), esc(dst), esc(label), _pen(m["amount"], hi), esc(tip), esc(tip)))

    lines.append("}")
    return RingPicture(dot="\n".join(lines), shown_edges=len(edges), total_edges=total, roles=roles)


def _html(text) -> str:
    """Safe inside a Graphviz HTML-like label."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
