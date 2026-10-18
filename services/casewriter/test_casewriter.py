"""
Tests for the case writer. Run from the repo root, either way:
    python -m pytest -q services/casewriter
    python -m services.casewriter.test_casewriter
Every line should say PASS. No internet, Redis or Granite needed.
"""
import json
from pathlib import Path

from services.casewriter.facts import parse_ring, build_facts
from services.casewriter.template import template_summary, REQUIRED_KEYS
from services.casewriter.factcheck import check_reply
from services.casewriter.casewriter import write_case, ring_to_stream_fields, Stats

FAKE_RINGS = Path(__file__).with_name("fake_rings.json")

with open(FAKE_RINGS) as fh:
    RINGS = json.load(fh)


def ring_fields(i):
    return ring_to_stream_fields(RINGS[i])


def facts_for(i):
    ring = parse_ring(ring_fields(i))
    return ring, build_facts(ring)


def test_facts_scatter_gather():
    _, f = facts_for(0)
    assert f["shape"] == "scatter-gather"
    assert f["key_account"] == "10_8000EBD30"
    assert f["key_received_usd"] == 10000
    assert f["passed_on_pct"] == 98
    assert f["hours_to_pass_on"] == 0.7
    assert f["key_payees"] == 6
    assert f["collector"] == "27_80A1C4F20"
    assert f["origin"] == "27_SRC0001A0"


def test_facts_fan_out_edges_as_lists():
    _, f = facts_for(1)
    assert f["key_account"] == "3_A00F12C40"
    assert f["passed_on_pct"] is None        # it never received money
    assert f["key_payees"] == 7
    assert f["collector"] is None


def test_facts_cycle_shape_name_cleaned():
    _, f = facts_for(2)
    assert f["shape"] == "cycle"
    assert f["collector"] is None and f["origin"] is None


def test_template_has_all_parts_and_passes_own_check():
    for i in range(len(RINGS)):
        ring, f = facts_for(i)
        s = template_summary(f)
        assert list(s) == REQUIRED_KEYS
        ok, why = check_reply(s, f, ring["accounts"])
        assert ok, "ring %d template failed its own check: %s" % (i, why)


def test_check_catches_changed_number():
    ring, f = facts_for(0)
    s = template_summary(f)
    s["summary"] = s["summary"].replace("$10,000", "$12,500")
    ok, why = check_reply(s, f, ring["accounts"])
    assert not ok and "12500" in why


def test_check_allows_rounding():
    ring, f = facts_for(0)
    s = template_summary(f)
    s["summary"] = "Account 10_8000EBD30 received about $10k and passed on 98% within 1 hour. 9 accounts were involved."
    ok, why = check_reply(s, f, ring["accounts"])
    assert ok, why


def test_check_catches_invented_account():
    ring, f = facts_for(0)
    s = template_summary(f)
    s["what_happened"] += " Account 99_DEADBEEF1 was also involved."
    ok, why = check_reply(s, f, ring["accounts"])
    assert not ok and "99_DEADBEEF1" in why


def test_check_catches_wrong_date():
    ring, f = facts_for(0)
    s = template_summary(f)
    s["summary"] += " It began on 2022-09-03."
    ok, why = check_reply(s, f, ring["accounts"])
    assert not ok and "date" in why


def test_check_catches_missing_part():
    ring, f = facts_for(0)
    s = template_summary(f)
    del s["possible_victim"]
    ok, why = check_reply(s, f, ring["accounts"])
    assert not ok and "possible_victim" in why


def test_why_suspicious_uses_behaviour_not_duration():
    ring = dict(RINGS[0], xgb_score=None, gnn_score=None, reasons=[])  # no models yet
    parsed = parse_ring(ring_to_stream_fields(ring))
    f = build_facts(parsed)
    s = template_summary(f)
    assert "took place over" not in s["why_suspicious"]
    assert "passed on 98%" in s["why_suspicious"]
    assert "6 different accounts" in s["why_suspicious"]
    ok, why = check_reply(s, f, parsed["accounts"])
    assert ok, why


def test_structuring_signal():
    ring = dict(RINGS[1])
    ring["edges"] = [[e[0], e[1], e[2], 9500 + i * 50, e[4], e[5]]
                     for i, e in enumerate(RINGS[1]["edges"])]
    parsed = parse_ring(ring_to_stream_fields(ring))
    f = build_facts(parsed)
    s = template_summary(f)
    assert "just under $10,000" in s["why_suspicious"], s["why_suspicious"]
    assert "similar in size" in s["why_suspicious"]
    ok, why = check_reply(s, f, parsed["accounts"])
    assert ok, why


def test_case_without_granite_uses_template():
    case = write_case(ring_fields(0), use_granite=False)
    assert case["source"] == "template"
    assert set(json.loads(case["summary"])) == set(REQUIRED_KEYS)
    for value in case.values():
        assert isinstance(value, str)      # Redis needs text values


def test_good_granite_reply_is_accepted():
    _, f = facts_for(0)
    good = template_summary(f)             # a reply that only uses real facts
    fake_granite = lambda facts: "```json\n" + json.dumps(good) + "\n```"
    stats = Stats()
    case = write_case(ring_fields(0), True, stats, granite_fn=fake_granite)
    assert case["source"] == "granite", case["check_note"]
    assert stats.granite_passed == 1


def test_lying_granite_reply_falls_back():
    _, f = facts_for(0)
    bad = template_summary(f)
    bad["summary"] = bad["summary"].replace("98%", "75%")
    fake_granite = lambda facts: json.dumps(bad)
    case = write_case(ring_fields(0), True, granite_fn=fake_granite)
    assert case["source"] == "template"
    assert "rejected" in case["check_note"]


def test_granite_crash_falls_back():
    def broken(facts):
        raise ConnectionError("watsonx unreachable")
    case = write_case(ring_fields(0), True, granite_fn=broken)
    assert case["source"] == "template" and "error" in case["check_note"]


def test_redis_end_to_end():
    try:
        import fakeredis
    except ImportError:
        try:
            import pytest
            pytest.skip("pip install fakeredis to run it")
        except ImportError:
            print("SKIP  test_redis_end_to_end (pip install fakeredis to run it)")
            return "skip"
    from services.casewriter.casewriter import push_fake, run_redis
    r = fakeredis.FakeRedis(decode_responses=True)
    push_fake(str(FAKE_RINGS), r)
    run_redis(use_granite=False, r=r, max_loops=1)
    cases = r.xrange("cases")
    assert len(cases) == 3
    assert {fields["ring_id"] for _id, fields in cases} == {"R-0001", "R-0002", "R-0003"}


def test_service_publishes_template_first_then_the_granite_upgrade():
    """The service writes the template at once and a Granite case afterwards from its worker."""
    try:
        import fakeredis
    except ImportError:
        import pytest
        pytest.skip("pip install fakeredis to run it")
    from services.casewriter.casewriter import push_fake, run_redis
    r = fakeredis.FakeRedis(decode_responses=True)
    push_fake(str(FAKE_RINGS), r)
    # a "Granite" that answers with the template's own words: it passes the fact check
    stats = run_redis(use_granite=True, mode="all", r=r, max_loops=1,
                      granite_fn=lambda facts: json.dumps(template_summary(facts)))
    cases = [fields for _id, fields in r.xrange("cases")]
    assert len(cases) == 6
    for ring_id in ("R-0001", "R-0002", "R-0003"):
        mine = [c["source"] for c in cases if c["ring_id"] == ring_id]
        assert mine == ["template", "granite"], ring_id     # the template always lands first
    assert all(c["check_note"] == "passed fact check" for c in cases if c["source"] == "granite")
    assert stats.granite_tried == 3 and stats.granite_passed == 3


def test_service_keeps_the_template_when_granite_lies_or_fails():
    try:
        import fakeredis
    except ImportError:
        import pytest
        pytest.skip("pip install fakeredis to run it")
    from services.casewriter.casewriter import push_fake, run_redis
    r = fakeredis.FakeRedis(decode_responses=True)
    push_fake(str(FAKE_RINGS), r)

    def lying(facts):
        parts = template_summary(facts)
        parts["summary"] = "Account 99_FAKE moved $123,456,789 between the accounts."
        return json.dumps(parts)
    stats = run_redis(use_granite=True, mode="all", r=r, max_loops=1, granite_fn=lying)
    assert [fields["source"] for _id, fields in r.xrange("cases")] == ["template"] * 3
    assert stats.granite_tried == 3 and stats.granite_passed == 0

    r2 = fakeredis.FakeRedis(decode_responses=True)
    push_fake(str(FAKE_RINGS), r2)
    def crashing(facts):
        raise RuntimeError("model not loaded")
    stats = run_redis(use_granite=True, mode="all", r=r2, max_loops=1, granite_fn=crashing)
    assert [fields["source"] for _id, fields in r2.xrange("cases")] == ["template"] * 3
    assert stats.granite_passed == 0


def fake_redis():
    try:
        import fakeredis
    except ImportError:
        import pytest
        pytest.skip("pip install fakeredis to run it")
    return fakeredis.FakeRedis(decode_responses=True)


def version_of(i, version, extra_edges=()):
    """Fake ring i as a stream message with another version number (and maybe more edges)."""
    ring = dict(RINGS[i], version=version)
    ring["edges"] = list(ring["edges"]) + list(extra_edges)
    return ring_to_stream_fields(ring)


def test_facts_pass_on_all_and_its_sentence():
    ring, f = facts_for(0)
    assert f["hours_to_pass_on"] == 0.7 and f["hours_to_pass_on_all"] == 1.8   # first-out, last-out
    s = template_summary(f)
    assert "began passing it on within 0.7 hours and had moved 98% of it within 1.8 hours" in s["summary"]
    assert facts_for(1)[1]["hours_to_pass_on_all"] is None    # never received, so nothing passed on
    ok, why = check_reply(s, f, ring["accounts"])
    assert ok, why


def test_case_times_are_utc_with_ring_time_and_latency():
    from datetime import datetime, timezone
    import time
    case = write_case(ring_fields(0), use_granite=False)
    written = datetime.fromisoformat(case["written_at"])
    assert case["written_at"].endswith("+00:00") and written.utcoffset().total_seconds() == 0
    assert abs((datetime.now(timezone.utc) - written).total_seconds()) < 5
    assert case["ring_last_seen"] == RINGS[0]["last_seen"]
    assert case["written_after_s"] == ""                     # --file mode: no stream ID
    two_seconds_ago = "%d-0" % (time.time() * 1000 - 2000)
    case = write_case(ring_fields(0), use_granite=False, msg_id=two_seconds_ago)
    assert 1.9 <= float(case["written_after_s"]) < 5


def test_one_batch_keeps_only_the_newest_version_of_each_ring():
    from services.casewriter.casewriter import run_redis, IN_STREAM, GROUP
    r = fake_redis()
    for v in (1, 3, 2):
        r.xadd(IN_STREAM, version_of(0, v))
    r.xadd(IN_STREAM, version_of(1, 1))
    stats = run_redis(use_granite=False, r=r, max_loops=1, block_ms=10)
    cases = [(c["ring_id"], c["version"]) for _id, c in r.xrange("cases")]
    assert cases == [("R-0001", "3"), ("R-0002", "1")]
    assert stats.cases == 2
    assert r.xpending(IN_STREAM, GROUP)["pending"] == 0      # the skipped versions are acked too


def test_unchanged_facts_reuse_the_granite_summary():
    from services.casewriter.casewriter import Publisher, try_granite, CASE_KEY
    r = fake_redis()
    stats = Stats()
    p = Publisher(r, stats)
    echo = lambda facts: json.dumps(template_summary(facts))   # passes the fact check
    v1 = version_of(0, 1)
    assert p.template_first("1-0", v1, use_granite=True) == 1
    p.granite_result(v1, *try_granite(v1, stats, echo, "1-0"))
    # version 2 with the same transfers: no Granite call, the Granite text comes back at once
    assert p.template_first("2-0", version_of(0, 2), use_granite=True) is None
    last = r.xrevrange("cases", count=1)[0][1]
    assert (last["version"], last["source"], last["check_note"]) == ("2", "granite", "reused v1 summary")
    assert r.hgetall(CASE_KEY % "R-0001") == last
    # one more transfer is a new story: template first, Granite again
    extra = ["T-EXTRA", "10_8000EBD30", "27_80A1C4F20", 500.0, "2022-09-08T19:00:00", 0.5]
    assert p.template_first("3-0", version_of(0, 3, [extra]), use_granite=True) == 1
    assert r.hget(CASE_KEY % "R-0001", "check_note") == "granite pending"
    assert stats.granite_tried == 1 and stats.cases == 3


def test_granite_arriving_after_a_newer_unchanged_version_is_passed_on():
    from services.casewriter.casewriter import Publisher, try_granite, CASE_KEY
    r = fake_redis()
    stats = Stats()
    p = Publisher(r, stats)
    v1, v2 = version_of(0, 1), version_of(0, 2)
    p.template_first("1-0", v1, use_granite=True)
    p.template_first("2-0", v2, use_granite=True)            # Granite still busy with v1
    assert r.hget(CASE_KEY % "R-0001", "check_note") == "reused v1 summary"
    p.granite_result(v1, *try_granite(v1, stats, lambda f: json.dumps(template_summary(f)), "1-0"))
    held = r.hgetall(CASE_KEY % "R-0001")
    assert (held["version"], held["source"], held["check_note"]) == ("2", "granite", "reused v1 summary")
    assert [(c["version"], c["source"]) for _id, c in r.xrange("cases")] == [
        ("1", "template"), ("2", "template"), ("1", "granite"), ("2", "granite")]


def test_case_hash_never_goes_back_a_version():
    from services.casewriter.casewriter import store_case, CASE_KEY
    r = fake_redis()
    new = write_case(version_of(0, 5), use_granite=False)
    old = dict(write_case(version_of(0, 4), use_granite=False), source="granite")
    store_case(r, new)
    store_case(r, old)
    assert r.hget(CASE_KEY % "R-0001", "version") == "5" and r.xlen("cases") == 2


def test_pending_rings_are_finished_after_a_restart():
    import socket
    from services.casewriter.casewriter import push_fake, run_redis, IN_STREAM, GROUP
    r = fake_redis()
    push_fake(str(FAKE_RINGS), r)
    r.xgroup_create(IN_STREAM, GROUP, id="0")
    r.xreadgroup(GROUP, socket.gethostname(), {IN_STREAM: ">"}, count=10)   # read, then "crash"
    assert r.xpending(IN_STREAM, GROUP)["pending"] == 3
    run_redis(use_granite=False, r=r, max_loops=1, block_ms=10)
    assert {c["ring_id"] for _id, c in r.xrange("cases")} == {"R-0001", "R-0002", "R-0003"}
    assert r.xpending(IN_STREAM, GROUP)["pending"] == 0


def test_on_demand_mode_writes_templates_and_serves_requests_first():
    """Default mode: every ring gets the template; Granite only writes for a requested
    ring, found on the rings stream even when the service has restarted."""
    from services.casewriter.casewriter import push_fake, run_redis, CASE_KEY, NOTE_ON_REQUEST
    from services.page import granite_requests
    r = fake_redis()
    push_fake(str(FAKE_RINGS), r)
    echo = lambda facts: json.dumps(template_summary(facts))
    stats = run_redis(use_granite=True, mode="on_demand", r=r, max_loops=1, block_ms=10, granite_fn=echo)
    assert stats.granite_tried == 0
    assert all(c["source"] == "template" and c["check_note"] == NOTE_ON_REQUEST for _id, c in r.xrange("cases"))

    granite_requests.write(r, "R-0002", 1)
    stats = run_redis(use_granite=True, mode="on_demand", r=r, max_loops=1, block_ms=10, granite_fn=echo,
                      requests_from="0")
    assert stats.granite_tried == 1 and stats.granite_passed == 1
    assert r.hmget(CASE_KEY % "R-0002", "source", "check_note") == ["granite", "passed fact check"]
    assert r.hmget(CASE_KEY % "R-0001", "source") == ["template"]


def test_requests_are_ignored_once_granite_wrote_and_watched_rings_go_first():
    from services.casewriter.casewriter import (Publisher, Stats, try_granite, NOTE_PENDING,
                                                PRIORITY_REQUEST, MODE_ON_DEMAND)
    r = fake_redis()
    pub = Publisher(r, Stats())
    fields = ring_fields(0)
    assert pub.template_first("1-0", fields, True, MODE_ON_DEMAND) is None
    found = pub.request({"ring_id": "R-0001"})
    assert found is not None and found[1] == "1-0"
    assert r.hget("case:R-0001", "check_note") == NOTE_PENDING
    case, _note = try_granite(fields, granite_fn=lambda f: json.dumps(template_summary(f)), msg_id="1-0")
    pub.granite_result(fields, case, None)
    assert pub.request({"ring_id": "R-0001"}) is None          # already Granite's text
    changed = dict(fields, version="2", edges=json.dumps([dict(e, amount_usd=e["amount_usd"] + 5)
                                                           for e in RINGS[0]["edges"]]))
    assert pub.template_first("2-0", changed, True, MODE_ON_DEMAND) == PRIORITY_REQUEST
    assert pub.request({"ring_id": "R-9999"}) is None            # unknown ring


def test_worker_never_queues_the_same_ring_version_twice():
    import threading
    from services.casewriter.casewriter import GraniteWorker, Stats
    r = fake_redis()
    gate = threading.Event()

    def slow(facts):
        gate.wait(5)
        return json.dumps(template_summary(facts))
    worker = GraniteWorker(r, Stats(), slow, workers=1)
    assert worker.submit(ring_fields(0), "1-0")
    assert not worker.submit(ring_fields(0), "1-0")
    gate.set()
    worker.drain()
    assert worker.submit(ring_fields(0), "1-0")
    worker.drain()


def test_case_hash_and_stats_hash():
    from services.casewriter.casewriter import push_fake, run_redis, CASE_KEY, STATS_KEY
    r = fake_redis()
    push_fake(str(FAKE_RINGS), r)
    run_redis(use_granite=True, mode="all", r=r, max_loops=1, block_ms=10,
              granite_fn=lambda facts: json.dumps(template_summary(facts)))
    for ring_id in ("R-0001", "R-0002", "R-0003"):
        newest = [c for _id, c in r.xrange("cases") if c["ring_id"] == ring_id][-1]
        assert r.hgetall(CASE_KEY % ring_id) == newest and newest["source"] == "granite"
    assert set(newest) == {"ring_id", "version", "summary", "source", "check_note", "written_at",
                           "facts", "ring_last_seen", "written_after_s"}
    assert r.hgetall(STATS_KEY) == {"cases_written": "3", "granite_tried": "3", "granite_passed": "3"}

    # a rejected reply leaves the template in place and the hash says why
    r = fake_redis()
    push_fake(str(FAKE_RINGS), r)
    run_redis(use_granite=True, mode="all", r=r, max_loops=1, block_ms=10,
              granite_fn=lambda facts: json.dumps(dict(template_summary(facts), summary="It moved $999,999.")))
    held = r.hgetall(CASE_KEY % "R-0001")
    assert held["source"] == "template" and held["check_note"].startswith("granite reply rejected")
    assert r.hgetall(STATS_KEY) == {"cases_written": "3", "granite_tried": "3", "granite_passed": "0"}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self.body, self.reason = status, body, "status %d" % status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def with_fake_watsonx(chat_status, run):
    """Run `run(calls)` with watsonx settings and a stand-in for `requests` that records calls."""
    import os
    import sys
    import types
    from services.casewriter import granite
    calls = []

    def post(url, timeout=None, **kw):
        calls.append((url, timeout))
        if "iam.cloud.ibm.com" in url:
            return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        if chat_status == 200:
            return FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})
        return FakeResponse(chat_status, {"errors": [{"code": "model_not_supported"}]})

    names = ("WATSONX_APIKEY", "WATSONX_PROJECT_ID", "WATSONX_URL")
    saved_env = {k: os.environ.get(k) for k in names}
    saved_requests = sys.modules.get("requests")
    os.environ.update({"WATSONX_APIKEY": "k", "WATSONX_PROJECT_ID": "p", "WATSONX_URL": "https://x"})
    sys.modules["requests"] = types.SimpleNamespace(post=post)
    granite.reset_state()
    try:
        run(calls)
    finally:
        granite.reset_state()
        if saved_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = saved_requests
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_granite_token_is_reused_and_calls_time_out_at_15s():
    from services.casewriter import granite

    def run(calls):
        granite.call_granite({"ring_id": "R-1"})
        granite.call_granite({"ring_id": "R-1"})
        iam = [c for c in calls if "iam" in c[0]]
        chat = [c for c in calls if "/text/chat" in c[0]]
        assert len(iam) == 1 and len(chat) == 2
        assert all(timeout == 15 for _url, timeout in calls)
    with_fake_watsonx(200, run)


def test_granite_bills_a_space_instead_of_the_project_when_one_is_set():
    """WATSONX_SPACE_ID (a deployment space made through the REST API) replaces project_id in
    the call; without it the project is sent, and neither set means not configured."""
    import os
    import sys
    import types
    from services.casewriter import granite

    bodies = []

    def post(url, timeout=None, json=None, **kw):
        bodies.append(json)
        if "iam.cloud.ibm.com" in url:
            return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        return FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})

    names = ("WATSONX_APIKEY", "WATSONX_PROJECT_ID", "WATSONX_SPACE_ID", "WATSONX_URL")
    saved_env = {k: os.environ.get(k) for k in names}
    saved_requests = sys.modules.get("requests")
    sys.modules["requests"] = types.SimpleNamespace(post=post)
    granite.reset_state()
    try:
        os.environ.update({"WATSONX_APIKEY": "k", "WATSONX_PROJECT_ID": "p", "WATSONX_URL": "https://x"})
        os.environ.pop("WATSONX_SPACE_ID", None)
        granite.call_granite({"ring_id": "R-1"})
        chat = [b for b in bodies if b is not None][-1]
        assert chat["project_id"] == "p" and "space_id" not in chat

        os.environ["WATSONX_SPACE_ID"] = "s"
        granite.call_granite({"ring_id": "R-1"})
        chat = [b for b in bodies if b is not None][-1]
        assert chat["space_id"] == "s" and "project_id" not in chat

        os.environ.pop("WATSONX_PROJECT_ID")
        assert granite.watsonx_configured()
        os.environ.pop("WATSONX_SPACE_ID")
        assert not granite.watsonx_configured()
    finally:
        granite.reset_state()
        if saved_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = saved_requests
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_granite_404_switches_it_off_for_every_later_case():
    from services.casewriter import granite

    def run(calls):
        case = write_case(ring_fields(0), use_granite=True)
        assert case["source"] == "template"
        assert case["check_note"] == "granite disabled: 404 model_not_supported"
        n = len(calls)
        case = write_case(ring_fields(1), use_granite=True)
        assert case["check_note"] == "granite disabled: 404 model_not_supported"
        assert len(calls) == n                                 # no second try
        assert granite.disabled_reason() == "404 model_not_supported"
    with_fake_watsonx(404, run)


def test_granite_401_gets_one_fresh_token_then_switches_off():
    from services.casewriter import granite

    def run(calls):
        try:
            granite.call_granite({"ring_id": "R-1"})
            assert False, "a 401 should raise"
        except granite.GraniteDisabled as exc:
            assert str(exc).startswith("granite disabled: 401")
        assert [("iam" in url) for url, _t in calls] == [True, False, True, False]
    with_fake_watsonx(401, run)


def test_granite_backend_selection_without_credentials_or_model(monkeypatch=None):
    import os
    from services.casewriter import granite
    saved = {k: os.environ.pop(k, None) for k in ("WATSONX_APIKEY", "WATSONX_API_KEY", "WATSONX_PROJECT_ID",
                                                   "WATSONX_URL", "GRANITE_GGUF")}
    try:
        os.environ["GRANITE_GGUF"] = "/nowhere/granite.gguf"   # an explicit path that does not exist: no search
        assert granite.local_model_path() == ""
        assert granite.granite_backend() == ""
        assert not granite.granite_configured()
        try:
            granite.call_granite({"ring_id": "R-1"})
            assert False, "should refuse without any backend"
        except RuntimeError as exc:
            assert "not configured" in str(exc)
        os.environ.update({"WATSONX_APIKEY": "k", "WATSONX_PROJECT_ID": "p", "WATSONX_URL": "https://x"})
        assert granite.granite_backend() == "watsonx"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if fn() != "skip":
                    print("PASS ", name)
            except AssertionError as exc:
                failed += 1
                print("FAIL ", name, "-", exc)
            except Exception as exc:
                if type(exc).__name__ == "Skipped":   # pytest.skip() when run as a script
                    print("SKIP ", name, "-", exc)
                    continue
                failed += 1
                print("ERROR", name, "-", repr(exc))
    print("\nAll tests passed." if not failed else "\n%d test(s) failed." % failed)
