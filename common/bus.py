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

STREAMS = (TXNS, SCORED, RINGS, CASES, DECISIONS)


def redis_host() -> str:
    return os.environ.get("REDIS_HOST", "redis")


def redis_port() -> int:
    return int(os.environ.get("REDIS_PORT", "6379"))


def connect():
    """A redis-py client that returns text (not bytes) for every value."""
    import redis  # imported here so the cleaning scripts never need redis installed

    return redis.Redis(host=redis_host(), port=redis_port(), decode_responses=True)
