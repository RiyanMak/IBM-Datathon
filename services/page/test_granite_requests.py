"""Tests for the page's Granite requests. Uses fakeredis."""
from datetime import datetime, timezone

import pytest

fakeredis = pytest.importorskip("fakeredis")

from common import bus
from services.page import granite_requests as gr
from services.page.models import CaseView


def case(source="template", note="granite on request"):
    return CaseView(ring_id="R-1", version=1, parts=[], source=source, check_note=note,
                    written_at="2026-10-18T12:00:00+00:00", facts=None, received_ms=0)


def test_build_and_write():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    fields = gr.build("R-1", "3", "page", now)
    assert fields == {"ring_id": "R-1", "version": "3", "requested_at": "2026-10-18T12:00:00.000+00:00",
                      "requested_by": "page"}
    with pytest.raises(ValueError):
        gr.build("", 1)
    r = fakeredis.FakeRedis(decode_responses=True)
    gr.write(r, "R-2", 1, "flask")
    assert r.xrange(bus.GRANITE_REQUESTS)[0][1]["requested_by"] == "flask"


def test_wanted_and_expected():
    assert gr.wanted(None) and gr.wanted(case()) and not gr.wanted(case("granite", "passed fact check"))
    assert gr.granite_expected(case()) and gr.granite_expected(case(note="granite pending"))
    assert not gr.granite_expected(case(note="granite not used"))
    assert not gr.granite_expected(case("granite", "passed fact check")) and not gr.granite_expected(None)


def test_throttle_asks_once_per_version_until_the_repeat_time():
    r = fakeredis.FakeRedis(decode_responses=True)
    t = gr.Throttle(repeat_s=45)
    assert t.request(r, "R-1", 1, now=100.0) and not t.request(r, "R-1", 1, now=120.0)
    assert t.asked_at("R-1", 1) == 100.0 and t.asked_at("R-1", 2) is None
    assert t.request(r, "R-1", 2, now=121.0)          # a new version is asked for at once
    assert t.request(r, "R-1", 2, now=170.0)          # and again after 45 s
    assert r.xlen(bus.GRANITE_REQUESTS) == 3
