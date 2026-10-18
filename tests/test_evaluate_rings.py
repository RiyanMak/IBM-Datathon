"""Tests for scripts/evaluate_rings.py."""
import json
from datetime import datetime

from scripts.evaluate_rings import evaluate, load_attempts, parse_ring_message, report, rings_from_file

PATTERNS = """\
BEGIN LAUNDERING ATTEMPT - SCATTER-GATHER
2022/09/09 10:00,010,M0000001,027,P0000001,1600.00,US Dollar,1600.00,US Dollar,ACH,1
2022/09/09 10:15,010,M0000001,027,P0000002,1600.00,US Dollar,1600.00,US Dollar,ACH,1
2022/09/09 10:30,010,M0000001,027,P0000003,1600.00,US Dollar,1600.00,US Dollar,ACH,1
2022/09/09 12:00,027,P0000001,027,B0000001,1590.00,US Dollar,1590.00,US Dollar,ACH,1
2022/09/09 12:10,027,P0000002,027,B0000001,1590.00,US Dollar,1590.00,US Dollar,ACH,1
2022/09/09 12:20,027,P0000003,027,B0000001,1590.00,US Dollar,1590.00,US Dollar,ACH,1
END LAUNDERING ATTEMPT
BEGIN LAUNDERING ATTEMPT - CYCLE: Max 4 hops
2022/09/09 20:00,005,C1,006,C2,100.00,Euro,100.00,Euro,Wire,1
2022/09/09 21:00,006,C2,007,C3,98.00,Euro,98.00,Euro,Wire,1
2022/09/09 22:00,007,C3,005,C1,96.00,Euro,96.00,Euro,Wire,1
END LAUNDERING ATTEMPT
BEGIN LAUNDERING ATTEMPT - FAN-OUT
2022/09/05 09:00,003,OLD1,020,OLD2,500.00,US Dollar,500.00,US Dollar,ACH,1
2022/09/05 09:05,003,OLD1,021,OLD3,500.00,US Dollar,500.00,US Dollar,ACH,1
2022/09/05 09:10,003,OLD1,022,OLD4,500.00,US Dollar,500.00,US Dollar,ACH,1
END LAUNDERING ATTEMPT
"""


def ring(ring_id, version, shape, accounts, published_at):
    return {"ring_id": ring_id, "version": str(version), "shape": shape, "n_accounts": str(len(accounts)),
            "accounts": json.dumps(accounts), "edges": "[]", "xgb_score": "0.9", "gnn_score": "",
            "reasons": "[]", "first_seen": published_at, "last_seen": published_at, "published_at": published_at}


def test_load_attempts_parses_shapes_accounts_money_and_times(tmp_path):
    path = tmp_path / "patterns.txt"
    path.write_text(PATTERNS)
    attempts = load_attempts(path, {"Euro": 1.1})
    assert [a["shape"] for a in attempts] == ["scatter-gather", "cycle", "fan-out"]
    sg = attempts[0]
    assert sg["accounts"] == {"10_M0000001", "27_P0000001", "27_P0000002", "27_P0000003", "27_B0000001"}
    assert sg["first_ts"] == datetime(2022, 9, 9, 10, 0) and sg["last_ts"] == datetime(2022, 9, 9, 12, 20)
    assert sg["total_usd"] == 3 * 1600 + 3 * 1590 and sg["money_known"]
    assert attempts[1]["total_usd"] == (100 + 98 + 96) * 1.1
    # unknown currency: counted, but money unknown
    attempts = load_attempts(path, {})
    assert attempts[1]["money_known"] is False


def test_evaluate_caught_time_to_alert_and_stoppable_money(tmp_path):
    path = tmp_path / "patterns.txt"
    path.write_text(PATTERNS)
    attempts = load_attempts(path, {"Euro": 1.1})
    since = datetime(2022, 9, 8, 16, 12)

    rings = [parse_ring_message(m) for m in [
        # version 1 at 10:40: M and the three payees, no collector yet: 4 of 4 ring accounts are in the attempt
        ring("R-0001", 1, "fan-out", ["10_M0000001", "27_P0000001", "27_P0000002", "27_P0000003"], "2022-09-09T10:40:00"),
        # version 2 at 12:30: the full scatter-gather
        ring("R-0001", 2, "scatter-gather", ["10_M0000001", "27_P0000001", "27_P0000002", "27_P0000003", "27_B0000001"], "2022-09-09T12:30:00"),
        # a ring of 6 accounts of which only 2 belong to the cycle: not "caught" (2 < 3), but "covered"? no (2 of 3 attempt accounts = 2/3 >= half: yes covered)
        ring("R-0002", 1, "other", ["5_C1", "6_C2", "70_X1", "70_X2", "70_X3", "70_X4"], "2022-09-09T22:30:00"),
        # an unrelated ring
        ring("R-0003", 1, "fan-in", ["70_Y1", "70_Y2", "70_Y3"], "2022-09-09T23:00:00"),
    ]]
    s = evaluate(attempts, rings, since=since)
    assert s["attempts_in_period"] == 2            # the fan-out on Sept 5 is before the period
    assert s["caught"] == 1 and s["caught_share"] == 0.5
    assert s["covered"] == 2
    c = s["caught_attempts"][0]
    assert c["ring"] == "R-0001" and c["minutes_to_alert"] == 40.0     # first catching version, 10:40 - 10:00
    assert c["shape_match"] is False                                     # fan-out at the time of the alert
    assert c["shape_match_final"] is True                                # scatter-gather by version 2
    assert s["shape_matched"] == 0 and s["shape_matched_final"] == 1
    # money after 10:40: the three 1,590 payments
    assert abs(s["still_stoppable_median"] - (3 * 1590) / (3 * 1600 + 3 * 1590)) < 1e-9
    assert s["rings_total"] == 3 and s["rings_matching_an_attempt"] == 1
    assert s["per_shape"]["scatter-gather"] == {"attempts": 1, "caught": 1, "shape_matched": 0, "shape_matched_final": 1}
    assert s["per_shape"]["cycle"] == {"attempts": 1, "caught": 0, "shape_matched": 0, "shape_matched_final": 0}
    text = report(s)
    assert "| Caught by a ring" in text and "50%" in text


def test_rings_file_and_missing_published_at_fall_back_to_last_seen(tmp_path):
    m = ring("R-0009", 1, "cycle", ["5_C1", "6_C2", "7_C3"], "2022-09-09T22:30:00")
    del m["published_at"]
    path = tmp_path / "rings.jsonl"
    path.write_text(json.dumps(m) + "\n\n")
    rings = rings_from_file(path)
    assert rings[0]["published_at"] == datetime(2022, 9, 9, 22, 30) and rings[0]["shape"] == "cycle"
