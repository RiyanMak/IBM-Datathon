"""One place for the Redis connection and the stream names.

Every service (replayer, scorer, rings, casewriter, page) imports from here so
nobody can drift on the host name or on how a stream is spelled.

    from common.bus import connect, RINGS, CASES
    r = connect()
    r.xadd(CASES, {...})

Inside Docker Compose the host is "redis" (the service name). On a laptop set
REDIS_HOST=localhost in the shell or in .env.
"""

import os

TXNS = "txns"          # replayer -> scorer
SCORED = "scored"      # scorer -> rings
RINGS = "rings"        # rings -> casewriter, page
CASES = "cases"        # casewriter -> page
DECISIONS = "decisions"  # page -> (audit)
GRANITE_REQUESTS = "granite_requests"  # page -> casewriter: "write the Granite summary for this ring now"

STREAMS = (TXNS, SCORED, RINGS, CASES, DECISIONS, GRANITE_REQUESTS)


def redis_host() -> str:
    return os.environ.get("REDIS_HOST", "redis")


def redis_port() -> int:
    return int(os.environ.get("REDIS_PORT", "6379"))


def connect(**options):
    """A redis-py client that returns text (not bytes) for every value.

    Extra keyword options go straight to redis.Redis, e.g. the page passes a short
    socket_connect_timeout so a down Redis shows a banner instead of a stall.
    """
    import redis  # imported here so the cleaning scripts never need redis installed

    options.setdefault("decode_responses", True)
    return redis.Redis(host=redis_host(), port=redis_port(), **options)
