"""Tests for the case packet (services/notifier/packet.py). No Redis, no network.

Cases are built the way the live service builds them: the case writer's write_case on
the fake rings, template only.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.casewriter.casewriter import ring_to_stream_fields, write_case
from services.notifier import packet

FAKE_RINGS = Path(__file__).resolve().parents[1] / "casewriter" / "fake_rings.json"
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


def fake_rings():
    with open(FAKE_RINGS) as fh:
        return [ring_to_stream_fields(r) for r in json.load(fh)]


def case_and_ring(ring_fields):
    return write_case(ring_fields, use_granite=False), ring_fields


@pytest.mark.parametrize("ring_fields", fake_rings(), ids=lambda r: r["ring_id"])
def test_packet_from_each_fake_ring_has_parts_txn_ids_and_only_known_numbers(ring_fields):
    case, ring = case_and_ring(ring_fields)
    text = packet.render(case, ring, [], now=NOW)
    for label in packet.PART_LABELS.values():
        assert "### " + label in text
    for edge in json.loads(ring["edges"]):   # dict edges, or the list form [txn_id, from, ...]
        txn_id = edge["txn_id"] if isinstance(edge, dict) else edge[0]
        assert "| %s |" % txn_id in text
    assert packet.check_numbers(text, json.loads(case["facts"]), ring) == []
    # the sections come in the plan's order
    heads = [packet.H_SUMMARY, packet.H_FACTS, packet.H_ACCOUNTS, packet.H_TRANSFERS,
             packet.H_LINEAGE, packet.H_DECISIONS, packet.H_DISCLAIMER]
    assert [text.index(h) for h in heads] == sorted(text.index(h) for h in heads)
    assert "Template" in text and "Generated at | 2026-10-18 12:00:00 UTC" in text
    facts = json.loads(case["facts"])
    assert "| %s | possible victim (key account) |" % facts["key_account"] in text


def test_check_numbers_catches_an_invented_number():
    case, ring = case_and_ring(fake_rings()[0])
    summary = json.loads(case["summary"])
    summary["summary"] += " About 777,777 dollars more went missing."
    case["summary"] = json.dumps(summary)
    text = packet.render(case, ring, [])
    assert packet.check_numbers(text, json.loads(case["facts"]), ring) == ["777777"]


def test_transfers_capped_at_200_rows_with_a_note():
    ring = fake_rings()[0]
    edges = [{"txn_id": str(5000000 + i), "from": "10_A%03d" % (i % 7), "to": "20_B%03d" % (i % 11),
              "amount_usd": 100.0 + i, "ts": "2022-09-09T%02d:%02d:00" % (i // 60, i % 60), "score": 0.9}
             for i in range(250)]
    ring["edges"] = json.dumps(list(reversed(edges)))   # out of order on purpose
    case = write_case(ring, use_granite=False)
    text = packet.render(case, ring, [])
    rows = [line for line in text.splitlines() if line.startswith("| 2022-09-09T")]
    assert len(rows) == 200
    assert "Showing the first 200 of 250 transfers" in text
    assert rows[0].startswith("| 2022-09-09T00:00:00 | 5000000 |")   # sorted by time
    assert "5000249" not in text


def test_no_ring_message_gives_the_no_transfer_line_and_named_accounts():
    case, _ring = case_and_ring(fake_rings()[0])
    text = packet.render(case, None, [])
    assert packet.NO_TRANSFERS in text
    facts = json.loads(case["facts"])
    assert facts["key_account"] in text.split(packet.H_ACCOUNTS)[1]


def test_decisions_in_time_order_with_labels():
    case, ring = case_and_ring(fake_rings()[1])
    decisions = [
        {"ring_id": case["ring_id"], "version": "1", "decision": "freeze", "note": "frozen after call",
         "decided_at": "2026-10-18T10:05:00.000+00:00", "decided_by": "jane", "channel": "slack"},
        {"ring_id": case["ring_id"], "version": "1", "decision": "more_info", "note": "check payees",
         "decided_at": "2026-10-18T09:00:00.000+00:00"},
        {"ring_id": "R-9999", "version": "1", "decision": "dismiss", "note": "other ring",
         "decided_at": "2026-10-18T09:30:00.000+00:00"},
    ]
    text = packet.render(case, ring, decisions)
    log = text.split(packet.H_DECISIONS)[1].split(packet.H_DISCLAIMER)[0]
    assert log.index("Needs more info") < log.index("Frozen")
    assert "| page | page | check payees |" in log
    assert "| jane | slack | frozen after call |" in log
    assert "other ring" not in log


def test_manifest_hash_overrides_the_reference_hash():
    case, ring = case_and_ring(fake_rings()[2])
    text = packet.render(case, ring, [], manifest={"input_sha256": "abc123"})
    assert "SHA-256 abc123)" in text
    default = packet.render(case, ring, [])
    from scripts.validate_clean import KEVIN_REFERENCE
    assert "SHA-256 %s)" % KEVIN_REFERENCE["input_sha256"] in default
    assert packet.FALLBACK_INPUT_SHA256 == KEVIN_REFERENCE["input_sha256"]


def test_filename():
    assert packet.filename({"ring_id": "R-0042", "version": "3"}) == "case_R-0042_v3.md"
    assert packet.filename({"ring_id": b"R-0007", "version": b"12"}) == "case_R-0007_v12.md"


def test_bad_fields_never_raise():
    bad_case = {"ring_id": "R-0001", "version": "2", "summary": "{not json", "facts": "[1, 2]",
                "source": "granite", "written_at": ""}
    bad_ring = {"edges": "not json", "accounts": "{}"}
    text = packet.render(bad_case, bad_ring, [{"decision": "freeze"}, "junk", None])
    assert "(not available)" in text and "No transfer detail available" in text
    assert "Granite, fact-checked" in text
    assert packet.render({}, None, None)   # still a packet
    edges_ring = {"edges": json.dumps([{"txn_id": 1, "ts": "2022-09-09T10:00:00", "amount_usd": "x"},
                                       "junk", ["7", "a", "b", "12.5", "2022-09-09T09:00:00"]])}
    text = packet.render({"ring_id": "R-1"}, edges_ring, [])
    assert text.index("| 7 |") < text.index("| 1 |")
    assert packet.check_numbers(text, "{broken", edges_ring) is not None


def test_load_manifest(tmp_path):
    path = tmp_path / "cleaning_manifest.json"
    path.write_text(json.dumps({"input_sha256": "feed"}))
    assert packet.load_manifest(path) == {"input_sha256": "feed"}
    path.write_text("{oops")
    assert packet.load_manifest(path) is None
    assert packet.load_manifest(tmp_path / "missing.json") is None
