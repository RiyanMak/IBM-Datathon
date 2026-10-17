"""
Tests for the case writer. Run from inside the casewriter folder:
    python3 test_casewriter.py
Every line should say PASS. No internet, Redis or Granite needed.
"""
import json

from facts import parse_ring, build_facts
from template import template_summary, REQUIRED_KEYS
from factcheck import check_reply
from casewriter import write_case, ring_to_stream_fields, Stats

with open("fake_rings.json") as fh:
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
        print("SKIP  test_redis_end_to_end (pip install fakeredis to run it)")
        return "skip"
    from casewriter import push_fake, run_redis
    r = fakeredis.FakeRedis(decode_responses=True)
    push_fake("fake_rings.json", r)
    run_redis(use_granite=False, r=r, max_loops=1)
    cases = r.xrange("cases")
    assert len(cases) == 3
    assert {fields["ring_id"] for _id, fields in cases} == {"R-0001", "R-0002", "R-0003"}


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
                failed += 1
                print("ERROR", name, "-", repr(exc))
    print("\nAll tests passed." if not failed else "\n%d test(s) failed." % failed)
