"""Persistence contracts: acknowledged writes, retries, payload changes."""
import pytest

fakeredis = pytest.importorskip("fakeredis")
from common import bus
from services.page import decisions


def test_retry_is_idempotent_and_keeps_reviewed_version():
    r = fakeredis.FakeRedis(decode_responses=True)
    first = decisions.write(r, "R-1", 3, "freeze", "reviewed evidence", request_id="retry-1")
    assert first
    assert decisions.write(r, "R-1", 3, "freeze", "reviewed evidence", request_id="retry-1") == "already-recorded"
    rows = r.xrange(bus.DECISIONS)
    assert len(rows) == 1
    assert rows[0][1]["version"] == "3"
    assert rows[0][1]["decision"] == "freeze"


def test_reused_request_with_changed_payload_cannot_report_false_success():
    r = fakeredis.FakeRedis(decode_responses=True)
    decisions.write(r, "R-1", 3, "freeze", "a", request_id="retry-1")
    with pytest.raises(decisions.DecisionError):
        decisions.write(r, "R-1", 4, "dismiss", "b", request_id="retry-1")
    assert r.xlen(bus.DECISIONS) == 1
