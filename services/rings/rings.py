"""
Step 4 - the ring service (the grouping half).

Reads scored transfers from the Redis stream `scored`, keeps the ones at or
above the ring threshold for the last 24 data hours, regroups them every 10
data minutes into rings, names each ring's shape and publishes new or changed
rings to the stream `rings` (read by the case writer and the investigator page).

How to run (from the repo root):
  python -m services.rings.main           # the real service (Docker)
  python -m services.rings.main --once    # group what is on the stream now, then exit

The clock is data time (the `ts` of the transfers), never wall-clock time, so
the service behaves the same at every replay speed and in tests.

Grouping, as in the team guide:
  1. keep a transfer when score >= the ring threshold (models/thresholds.json)
  2. drop kept transfers older than 24 data hours
  3. every 10 data minutes: union-find over the kept transfers (sender and
     receiver merge). Accounts with more than 300 counterparties in the window
     are hubs and are left out, so a bank cannot glue unrelated rings together
  4. keep groups of 3 to 50 accounts; split bigger ones with Louvain and keep
     the pieces that fit
  5. a group that shares at least half its accounts with an existing ring keeps
     that ring_id and bumps the version; otherwise it is a new ring
  6. publish a ring when it is new or its accounts or transfers changed

The GNN second opinion is a later addition: gnn_score is sent empty until it
exists, and the page and the case writer both handle that.
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from common import bus
from common.features import to_minutes
from common.paths import MODELS_DIR

GROUP = "rings"
STATS_KEY = "stats:rings"
ID_KEY = "rings:next_id"       # INCR here gives ring IDs that survive restarts
BATCH = 500
BLOCK_MS = 1000
DEFAULT_MAXLEN = 50_000        # approximate cap on the rings stream
WINDOW_MINUTES = 24 * 60
REGROUP_MINUTES = 10
HUB_LIMIT = 300
MIN_SIZE = 3
MAX_SIZE = 50
RETIRE_MINUTES = 7 * 24 * 60   # forget a ring not seen in a group for this long
EXPAND_THRESHOLD = 0.5         # a ring member's transfers at or above this score join its ring
MAX_REASONS = 3
SHAPES = ("scatter-gather", "gather-scatter", "cycle", "stack", "bipartite", "fan-out", "fan-in", "other")


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- messages

class Edge:
    """One kept transfer: an arrow from src to dst."""
    __slots__ = ("txn_id", "src", "dst", "amount", "ts", "ts_min", "score", "reasons")

    def __init__(self, txn_id, src, dst, amount, ts, ts_min, score, reasons):
        self.txn_id = txn_id
        self.src = src
        self.dst = dst
        self.amount = amount
        self.ts = ts
        self.ts_min = ts_min
        self.score = score
        self.reasons = reasons


def parse_scored(fields) -> Edge:
    """A `scored` message (all text) -> Edge. Raises ValueError if it is malformed."""
    try:
        txn_id = str(int(fields["txn_id"]))
        ts = str(fields["ts"])
        ts_min = to_minutes(ts)
        src, dst = str(fields["from_acct"]), str(fields["to_acct"])
        amount = float(fields["amount_usd"])
        score = float(fields["score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("bad scored message: %s" % exc) from None
    if not src or not dst or not 0.0 <= score <= 1.0 or amount != amount:
        raise ValueError("bad scored message: empty account, score outside 0..1 or NaN amount")
    reasons = []
    raw = fields.get("reasons")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                reasons = [str(x) for x in parsed]
        except ValueError:
            pass
    return Edge(txn_id, src, dst, amount, ts, ts_min, score, reasons)


# ---------------------------------------------------------------- the window

class Window:
    """The kept transfers of the last `minutes` of data time, in arrival order."""

    def __init__(self, minutes=WINDOW_MINUTES):
        self.minutes = minutes
        self.edges = deque()
        self.ids = set()

    def add(self, edge: Edge) -> bool:
        """False if a transfer with this txn_id is already in the window."""
        if edge.txn_id in self.ids:
            return False
        self.edges.append(edge)
        self.ids.add(edge.txn_id)
        return True

    def prune(self, now_min: int) -> int:
        cutoff = now_min - self.minutes
        dropped = 0
        while self.edges and self.edges[0].ts_min < cutoff:
            self.ids.discard(self.edges.popleft().txn_id)
            dropped += 1
        return dropped

    def __len__(self):
        return len(self.edges)


# ---------------------------------------------------------------- grouping

def hub_accounts(edges, limit=HUB_LIMIT) -> set:
    """Accounts with more than `limit` distinct counterparties among the kept transfers."""
    counterparties = defaultdict(set)
    for e in edges:
        if e.src != e.dst:
            counterparties[e.src].add(e.dst)
            counterparties[e.dst].add(e.src)
    return {a for a, others in counterparties.items() if len(others) > limit}


def connected_groups(edges, exclude=frozenset()) -> list:
    """Union-find: every transfer merges its sender and receiver. Returns account sets."""
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        if e.src == e.dst or e.src in exclude or e.dst in exclude:
            continue
        parent.setdefault(e.src, e.src)
        parent.setdefault(e.dst, e.dst)
        ra, rb = find(e.src), find(e.dst)
        if ra != rb:
            parent[ra] = rb
    groups = defaultdict(set)
    for a in parent:
        groups[find(a)].add(a)
    return list(groups.values())


_louvain_warned = False


def split_large(group, edges, min_size=MIN_SIZE, max_size=MAX_SIZE) -> list:
    """Split a group with more than max_size accounts into communities (Louvain)."""
    global _louvain_warned
    try:
        import networkx as nx
    except ImportError:
        if not _louvain_warned:
            log("networkx is not installed: groups larger than %d accounts are dropped" % max_size)
            _louvain_warned = True
        return []
    graph = nx.Graph()
    for e in edges:
        if e.src in group and e.dst in group and e.src != e.dst:
            if graph.has_edge(e.src, e.dst):
                graph[e.src][e.dst]["weight"] += 1
            else:
                graph.add_edge(e.src, e.dst, weight=1)
    pieces = []
    for part in nx.community.louvain_communities(graph, weight="weight", seed=0):
        part = set(part)
        if min_size <= len(part) <= max_size:
            pieces.append(part)
    return pieces


def make_groups(edges, hub_limit=HUB_LIMIT, min_size=MIN_SIZE, max_size=MAX_SIZE):
    """Rings as account sets, plus the hubs that were left out."""
    hubs = hub_accounts(edges, hub_limit)
    groups = []
    for g in connected_groups(edges, exclude=hubs):
        if len(g) < min_size:
            continue
        if len(g) <= max_size:
            groups.append(g)
        else:
            groups.extend(split_large(g, edges, min_size, max_size))
    return groups, hubs


# ---------------------------------------------------------------- shapes

def adjacency(edges):
    out, inn = defaultdict(set), defaultdict(set)
    for e in edges:
        if e.src != e.dst:
            out[e.src].add(e.dst)
            inn[e.dst].add(e.src)
    return out, inn


def is_scatter_gather(out, inn) -> bool:
    """A sends to several middle accounts, and at least two of them send to the same B."""
    for a, middles in out.items():
        if len(middles) < 2:
            continue
        collectors = Counter()
        for m in middles:
            for b in out.get(m, ()):
                if b != a and b not in middles:
                    collectors[b] += 1
        if collectors and max(collectors.values()) >= 2:
            return True
    return False


def is_gather_scatter(out, inn) -> bool:
    """Several accounts send to one hub, which sends on to several others."""
    for h in set(out) & set(inn):
        senders = inn[h] - out[h]
        receivers = out[h] - inn[h]
        if len(senders) >= 2 and len(receivers) >= 2:
            return True
    return False


def has_cycle(out, min_length=3) -> bool:
    """Money goes around: some account is on a directed cycle of at least min_length accounts."""
    nodes = set(out) | {b for targets in out.values() for b in targets}
    reach = {}
    for a in nodes:
        seen, stack = set(), list(out.get(a, ()))
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(out.get(x, ()))
        reach[a] = seen
    for a in nodes:
        if a in reach[a]:
            on_cycle_with_a = {b for b in reach[a] if a in reach[b]}
            if len(on_cycle_with_a) >= min_length:
                return True
    return False


def layers(accounts, out, inn):
    sources = {a for a in accounts if out.get(a) and not inn.get(a)}
    sinks = {a for a in accounts if inn.get(a) and not out.get(a)}
    middles = {a for a in accounts if inn.get(a) and out.get(a)}
    return sources, middles, sinks


def is_stack(accounts, out, inn) -> bool:
    """Two bipartite layers in a row: sources -> middles -> sinks, at least 2 accounts each."""
    sources, middles, sinks = layers(accounts, out, inn)
    if len(sources) < 2 or len(middles) < 2 or len(sinks) < 2:
        return False
    for a, targets in out.items():
        for b in targets:
            if not ((a in sources and b in middles) or (a in middles and b in sinks)):
                return False
    return True


def is_bipartite(accounts, out, inn) -> bool:
    """Two groups, every arrow from the first to the second, with layering on both sides:
    at least 2 senders that each pay 2 or more, and at least 2 receivers each paid by 2 or more.
    (One account paying several others with one extra sender is a fan-out, not bipartite.)"""
    sources, middles, sinks = layers(accounts, out, inn)
    if middles or len(sources) < 2 or len(sinks) < 2:
        return False
    busy_sources = sum(1 for a in sources if len(out[a]) >= 2)
    busy_sinks = sum(1 for a in sinks if len(inn[a]) >= 2)
    return busy_sources >= 2 and busy_sinks >= 2


def name_shape(accounts, edges) -> str:
    """Most specific rule first. Stack and bipartite come before scatter-gather
    because a layered ring always contains small scatter-gather patterns."""
    out, inn = adjacency(edges)
    if is_stack(accounts, out, inn):
        return "stack"
    if is_bipartite(accounts, out, inn):
        return "bipartite"
    if is_scatter_gather(out, inn):
        return "scatter-gather"
    if is_gather_scatter(out, inn):
        return "gather-scatter"
    if has_cycle(out):
        return "cycle"
    if out and max(len(t) for t in out.values()) >= 3:
        return "fan-out"
    if inn and max(len(s) for s in inn.values()) >= 3:
        return "fan-in"
    return "other"


# ---------------------------------------------------------------- rings

class Ring:
    """A ring remembers every transfer and account it has ever been given. The 24-hour
    window is for discovering groups, not for forgetting them: once a transfer is part of
    a ring it stays, so the page and the case writer always see the whole case."""
    __slots__ = ("ring_id", "version", "accounts", "edges", "shape", "last_active_min")

    def __init__(self, ring_id, now_min):
        self.ring_id = ring_id
        self.version = 0
        self.accounts = set()
        self.edges = {}             # txn_id -> Edge
        self.shape = "other"
        self.last_active_min = now_min

    def absorb(self, ring_edges, accounts, max_size) -> bool:
        """Add what is new. New accounts are added only while the ring is under max_size;
        transfers between accounts already in the ring are always added. True if anything changed."""
        changed = False
        for e in sorted(ring_edges, key=lambda e: -e.score):
            if e.txn_id in self.edges:
                continue
            new_accounts = {e.src, e.dst} - self.accounts
            if new_accounts and len(self.accounts) + len(new_accounts) > max_size:
                continue
            self.accounts |= new_accounts
            self.edges[e.txn_id] = e
            changed = True
        return changed


def match_ring(group, rings, claimed) -> "Ring | None":
    """The existing ring that shares at least half of the group's accounts (the best one)."""
    best, best_share = None, 0.0
    for ring in rings.values():
        if ring.ring_id in claimed or not ring.accounts:
            continue
        share = len(group & ring.accounts) / len(group)
        if share >= 0.5 and share > best_share:
            best, best_share = ring, share
    return best


def expand_group(core, edges, hubs=frozenset(), max_size=MAX_SIZE):
    """The ring's transfers and accounts, starting from a core group.

    Every window transfer between two core accounts is in. Then one hop out: a
    transfer between a core account and any other account (a member's weaker-scoring
    transfer, kept in the window because it is above the expand threshold) joins too,
    highest score first, as long as the ring stays within max_size accounts. Hubs and
    self-transfers never join. Expanded accounts do not expand further.
    """
    inside, outward = [], []
    for e in edges:
        if e.src == e.dst or e.src in hubs or e.dst in hubs:
            continue
        a, b = e.src in core, e.dst in core
        if a and b:
            inside.append(e)
        elif a or b:
            outward.append(e)
    accounts = set(core)
    ring_edges = list(inside)
    for e in sorted(outward, key=lambda e: -e.score):
        other = e.dst if e.src in core else e.src
        if other not in accounts and len(accounts) >= max_size:
            continue
        accounts.add(other)
        ring_edges.append(e)
    return ring_edges, accounts


def top_reasons(edges, k=MAX_REASONS) -> list:
    """The most frequent reason phrases across the ring's transfers."""
    counts = Counter(phrase for e in edges for phrase in e.reasons)
    return [phrase for phrase, _ in counts.most_common(k)]


def ring_fields(ring: Ring, edges, published_at: str = "") -> dict:
    """The `rings` message, all values text, exactly what the case writer and page parse.

    published_at (extra field, data time) is when the ring service saw the group:
    the evaluation uses it for "minutes from an attempt's first transfer to its ring".
    """
    edges = sorted(edges, key=lambda e: (e.ts_min, int(e.txn_id)))
    accounts = sorted(ring.accounts)
    return {
        "published_at": published_at,
        "ring_id": ring.ring_id,
        "version": str(ring.version),
        "shape": ring.shape,
        "n_accounts": str(len(accounts)),
        "accounts": json.dumps(accounts),
        "edges": json.dumps([{"txn_id": e.txn_id, "from": e.src, "to": e.dst, "amount_usd": e.amount,
                              "ts": e.ts, "score": round(e.score, 4)} for e in edges]),
        "xgb_score": "%.4f" % (sum(e.score for e in edges) / len(edges)),
        "gnn_score": "",
        "reasons": json.dumps(top_reasons(edges)),
        "first_seen": edges[0].ts,
        "last_seen": edges[-1].ts,
    }


class RingService:
    """Feed it scored messages in order; it returns the rings to publish."""

    def __init__(self, threshold, window_minutes=WINDOW_MINUTES, regroup_minutes=REGROUP_MINUTES,
                 hub_limit=HUB_LIMIT, min_size=MIN_SIZE, max_size=MAX_SIZE, new_id=None,
                 expand_threshold=EXPAND_THRESHOLD):
        self.threshold = float(threshold)
        # a member's transfers at or above this score join its ring (never above the ring threshold)
        self.expand_threshold = min(float(expand_threshold), self.threshold)
        self.window = Window(window_minutes)
        self.regroup_minutes = regroup_minutes
        self.hub_limit, self.min_size, self.max_size = hub_limit, min_size, max_size
        self.rings = {}                 # ring_id -> Ring
        self.now_min = None             # data time of the newest message seen
        self.last_regroup_min = None
        self.dirty = False              # kept transfers arrived since the last regroup
        self._counter = 0
        self._new_id = new_id or self._local_id
        self.stats = Counter()

    def _local_id(self):
        self._counter += 1
        return "R-%04d" % self._counter

    def offer(self, fields) -> list:
        """One scored message in; the rings to publish out (usually none)."""
        try:
            edge = parse_scored(fields)
        except ValueError as exc:
            self.stats["skipped"] += 1
            log("skipping: %s" % exc)
            return []
        self.stats["seen"] += 1
        self.now_min = edge.ts_min if self.now_min is None else max(self.now_min, edge.ts_min)
        if self.last_regroup_min is None:
            self.last_regroup_min = self.now_min
        if edge.score >= self.expand_threshold and edge.src != edge.dst:
            if self.window.add(edge):
                self.stats["kept"] += 1
                self.dirty = True
            else:
                self.stats["duplicates"] += 1
        if self.now_min - self.last_regroup_min >= self.regroup_minutes:
            return self.regroup()
        return []

    def regroup(self) -> list:
        """Group the window now and return the rings that are new or changed."""
        if self.now_min is None:
            return []
        self.last_regroup_min = self.now_min
        self.dirty = False
        self.stats["regroups"] += 1
        self.window.prune(self.now_min)
        edges = list(self.window.edges)
        candidates = [e for e in edges if e.score >= self.threshold]
        groups, hubs = make_groups(candidates, self.hub_limit, self.min_size, self.max_size)
        self.stats["hubs"] = len(hubs)
        self.stats["candidates_in_window"] = len(candidates)

        published, claimed = [], set()
        for core in groups:
            ring_edges, accounts = expand_group(core, edges, hubs, self.max_size)
            if not ring_edges:
                continue
            ring = match_ring(core, self.rings, claimed)
            if ring is None:
                ring = Ring(self._new_id(), self.now_min)
                self.rings[ring.ring_id] = ring
            claimed.add(ring.ring_id)
            ring.last_active_min = self.now_min
            if ring.absorb(ring_edges, accounts, self.max_size):
                ring.version += 1
                ring.shape = name_shape(ring.accounts, ring.edges.values())
                published.append(ring_fields(ring, ring.edges.values(), self.data_time()))
                self.stats["published"] += 1

        for ring_id in [rid for rid, r in self.rings.items()
                        if self.now_min - r.last_active_min > RETIRE_MINUTES]:
            del self.rings[ring_id]
        return published

    def data_time(self) -> str:
        if self.now_min is None:
            return ""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.now_min * 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- Redis loop

def load_threshold(models_dir) -> float:
    with open(Path(models_dir) / "thresholds.json") as fh:
        return float(json.load(fh)["ring"])


def ensure_group(r, stream=bus.SCORED, group=GROUP):
    import redis

    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def publish(r, service, rings, msg_ids, stream, out_stream, maxlen, consumer):
    """XADD the rings, update stats:rings, XACK the messages, in one pipeline."""
    pipe = r.pipeline(transaction=False)
    for fields in rings:
        if maxlen:
            pipe.xadd(out_stream, fields, maxlen=maxlen, approximate=True)
        else:
            pipe.xadd(out_stream, fields)
    s = service.stats
    pipe.hset(STATS_KEY, mapping={
        "seen": str(s["seen"]), "kept_in_window": str(len(service.window)),
        "rings": str(len(service.rings)), "published": str(s["published"]),
        "regroups": str(s["regroups"]), "hubs": str(s["hubs"]), "skipped": str(s["skipped"]),
        "data_time": service.data_time(), "consumer": consumer,
    })
    if msg_ids:
        pipe.xack(stream, GROUP, *msg_ids)
    pipe.execute()


def describe(fields) -> str:
    return "ring %s v%s %s: %s accounts, %d transfers, xgb %s, %s .. %s" % (
        fields["ring_id"], fields["version"], fields["shape"], fields["n_accounts"],
        len(json.loads(fields["edges"])), fields["xgb_score"], fields["first_seen"], fields["last_seen"])


def run(r, service, consumer, stream=bus.SCORED, out_stream=bus.RINGS, batch=BATCH, block_ms=BLOCK_MS,
        maxlen=DEFAULT_MAXLEN, once=False, max_messages=None, stop_event=None, progress=log):
    """The grouping loop. Returns the service's stats counter."""
    ensure_group(r, stream)
    read_id = "0"         # my unacknowledged messages first, then new ones
    handled = 0

    def stopping():
        return stop_event is not None and stop_event.is_set()

    while not stopping():
        resp = r.xreadgroup(GROUP, consumer, {stream: read_id}, count=batch,
                            block=None if read_id == "0" else block_ms)
        entries = resp[0][1] if resp else []
        if not entries:
            if read_id == "0":
                read_id = ">"
                continue
            # quiet stream: group what arrived since the last regroup so nothing waits forever
            if service.dirty:
                rings = service.regroup()
                publish(r, service, rings, [], stream, out_stream, maxlen, consumer)
                for fields in rings:
                    if progress:
                        progress(describe(fields))
            if once:
                break
            continue
        rings = []
        for _msg_id, fields in entries:
            rings.extend(service.offer(fields))
        publish(r, service, rings, [msg_id for msg_id, _ in entries], stream, out_stream, maxlen, consumer)
        for fields in rings:
            if progress:
                progress(describe(fields))
        handled += len(entries)
        if max_messages is not None and handled >= max_messages:
            break

    if service.dirty:
        rings = service.regroup()
        publish(r, service, rings, [], stream, out_stream, maxlen, consumer)
        for fields in rings:
            if progress:
                progress(describe(fields))
    return service.stats


# ---------------------------------------------------------------- command line

def build_parser():
    p = argparse.ArgumentParser(description="Mule Ring Hunter ring service: scored -> rings")
    p.add_argument("--models", default=os.environ.get("MODELS_PATH", str(MODELS_DIR)),
                   help="folder with thresholds.json (default: %(default)s)")
    p.add_argument("--ring-threshold", type=float, help="use this score instead of thresholds.json")
    p.add_argument("--expand-threshold", type=float, default=EXPAND_THRESHOLD,
                   help="a ring member's other transfers at or above this score join the ring "
                        "(default: %(default)s; set it equal to the ring threshold to disable)")
    p.add_argument("--window-hours", type=float, default=WINDOW_MINUTES / 60,
                   help="keep transfers this many data hours (default: %(default)s)")
    p.add_argument("--regroup-minutes", type=int, default=REGROUP_MINUTES,
                   help="regroup every this many data minutes (default: %(default)s)")
    p.add_argument("--hub-limit", type=int, default=HUB_LIMIT,
                   help="accounts with more counterparties than this are left out (default: %(default)s)")
    p.add_argument("--min-size", type=int, default=MIN_SIZE, help="smallest ring (default: %(default)s)")
    p.add_argument("--max-size", type=int, default=MAX_SIZE, help="largest ring (default: %(default)s)")
    p.add_argument("--consumer", default=os.environ.get("RINGS_NAME") or socket.gethostname(),
                   help="consumer name in the group (default: $RINGS_NAME or the host name)")
    p.add_argument("--batch", type=int, default=BATCH, help="messages per read (default: %(default)s)")
    p.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN,
                   help="approximate cap on the rings stream (default: %(default)s; 0 = no cap)")
    p.add_argument("--stream", default=bus.SCORED, help="input stream (default: %(default)s)")
    p.add_argument("--out-stream", default=bus.RINGS, help="output stream (default: %(default)s)")
    p.add_argument("--once", action="store_true", help="group what is on the stream now, then exit")
    p.add_argument("--max-messages", type=int, help="stop after handling this many messages")
    return p


def main(argv=None, r=None):
    """Run the ring service. Tests pass argv and a fake Redis."""
    p = build_parser()
    args = p.parse_args(argv)
    if args.min_size < 2 or args.max_size < args.min_size or args.batch < 1:
        p.error("need 2 <= --min-size <= --max-size and --batch >= 1")

    threshold = args.ring_threshold if args.ring_threshold is not None else load_threshold(args.models)
    r = r or bus.connect()
    service = RingService(threshold, int(args.window_hours * 60), args.regroup_minutes, args.hub_limit,
                          args.min_size, args.max_size, new_id=lambda: "R-%04d" % r.incr(ID_KEY),
                          expand_threshold=args.expand_threshold)
    log("grouping '%s' -> '%s' as consumer '%s': ring threshold %.4f, expand threshold %.4f, window %gh, "
        "regroup every %d min, hubs > %d counterparties, rings of %d..%d accounts"
        % (args.stream, args.out_stream, args.consumer, threshold, service.expand_threshold,
           args.window_hours, args.regroup_minutes, args.hub_limit, args.min_size, args.max_size))

    stop = threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def on_signal(_signum, _frame):
            if stop.is_set():
                raise KeyboardInterrupt
            log("\nstopping after the current batch (signal again to quit at once)")
            stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, on_signal)
    try:
        stats = run(r, service, args.consumer, args.stream, args.out_stream, args.batch, BLOCK_MS,
                    args.maxlen, args.once, args.max_messages, stop_event=stop)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    log("done: seen %d | kept %d | rings %d | published %d | regroups %d | skipped %d"
        % (stats["seen"], stats["kept"], len(service.rings), stats["published"], stats["regroups"],
           stats["skipped"]))
    return service


if __name__ == "__main__":
    main()
