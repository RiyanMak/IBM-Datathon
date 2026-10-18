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


def test_label_summary_scores_rings_against_clean_csv_labels(tmp_path):
    from scripts.evaluate_rings import label_report, label_summary
    clean = tmp_path / "clean.csv"
    clean.write_text("txn_id,ts,label,split\n" + "\n".join(
        f"{i},2022-09-09T10:0{i % 10}:00,{1 if i in (1, 2, 3, 7) else 0},test" for i in range(10)) + "\n")
    msgs = [
        {"ring_id": "R-1", "version": "1", "shape": "fan-out", "edges": json.dumps(
            [{"txn_id": "1", "score": 0.99}, {"txn_id": "2", "score": 0.99}, {"txn_id": "5", "score": 0.6}])},
        {"ring_id": "R-1", "version": "2", "shape": "fan-out", "edges": json.dumps(
            [{"txn_id": "1", "score": 0.99}, {"txn_id": "2", "score": 0.99}, {"txn_id": "3", "score": 0.7}, {"txn_id": "5", "score": 0.6}])},
        {"ring_id": "R-2", "version": "1", "shape": "other", "edges": json.dumps(
            [{"txn_id": "8", "score": 0.97}, {"txn_id": "9", "score": 0.96}])},
    ]
    s = label_summary(msgs, clean, ring_threshold=0.95)
    assert s["rings"] == 2 and s["transfers_in_rings"] == 6 and s["laundering_in_rings"] == 3   # latest versions only
    assert s["rings_mostly_laundering"] == 1 and s["rings_without_laundering"] == 1
    assert s["laundering_in_period"] == 4 and s["laundering_covered"] == 3
    assert s["core_precision"] == 0.5 and s["expanded_precision"] == 0.5 and s["expanded_transfers"] == 2
    assert s["per_shape"]["fan-out"] == {"rings": 1, "mean_precision": 0.75}
    assert "| Rings (latest versions) | 2 |" in label_report(s)


def test_gnn_sweep_keeps_only_what_the_threshold_would_publish(tmp_path):
    from scripts.evaluate_rings import gnn_sweep, sweep_report
    path = tmp_path / "patterns.txt"
    path.write_text(PATTERNS)
    attempts = load_attempts(path, {"Euro": 1.1})
    clean = tmp_path / "clean.csv"
    clean.write_text("txn_id,ts,label,split\n1,2022-09-09T10:00:00,1,test\n2,2022-09-09T10:15:00,1,test\n"
                     "8,2022-09-09T23:00:00,0,test\n9,2022-09-09T23:05:00,0,test\n")
    from scripts.evaluate_rings import load_labels
    labels = load_labels(clean)

    caught = ring("R-0001", 1, "fan-out", ["10_M0000001", "27_P0000001", "27_P0000002", "27_P0000003"], "2022-09-09T10:40:00")
    caught["gnn_score"], caught["edges"] = "0.8000", json.dumps([{"txn_id": "1"}, {"txn_id": "2"}])
    false_alarm = ring("R-0003", 1, "fan-in", ["70_Y1", "70_Y2", "70_Y3"], "2022-09-09T23:00:00")
    false_alarm["gnn_score"], false_alarm["edges"] = "0.2000", json.dumps([{"txn_id": "8"}, {"txn_id": "9"}])
    unscored = ring("R-0004", 1, "other", ["70_Z1", "70_Z2", "70_Z3"], "2022-09-09T23:30:00")   # gnn_score ""

    rows = gnn_sweep([caught, false_alarm, unscored], [0.5, 0.9], attempts, datetime(2022, 9, 8, 16, 12), None, labels)
    base, half, high = rows
    assert base["gnn_threshold"] is None and base["rings"] == 3 and base["attempts_caught"] == 1
    assert base["rings_without_laundering"] == 2 and base["ring_transfer_precision"] == 0.5
    # at 0.5 the false alarm is gone; the unscored ring is never held back
    assert half["rings"] == 2 and half["attempts_caught"] == 1 and half["rings_without_laundering"] == 1
    assert half["ring_transfer_precision"] == 1.0 and half["rings_matching_an_attempt"] == 1
    # at 0.9 the GNN also drops the real ring: the cost shows up as a missed attempt
    assert high["rings"] == 1 and high["attempts_caught"] == 0
    text = sweep_report(rows)
    assert "| no GNN | 3 |" in text and "| 0.50 | 2 |" in text


def test_rings_file_and_missing_published_at_fall_back_to_last_seen(tmp_path):
    m = ring("R-0009", 1, "cycle", ["5_C1", "6_C2", "7_C3"], "2022-09-09T22:30:00")
    del m["published_at"]
    path = tmp_path / "rings.jsonl"
    path.write_text(json.dumps(m) + "\n\n")
    rings = rings_from_file(path)
    assert rings[0]["published_at"] == datetime(2022, 9, 9, 22, 30) and rings[0]["shape"] == "cycle"


def test_markdown_report_from_a_hand_made_summary():
    from scripts.evaluate_rings import markdown_report
    attempts = {"attempts_in_period": 10, "caught": 6, "caught_share": 0.6, "covered": 7,
                "shape_matched": 3, "shape_match_share": 0.5, "shape_matched_final": 5,
                "shape_match_final_share": 5 / 6, "minutes_to_alert_median": 22.0, "minutes_to_alert_mean": 30.5,
                "still_stoppable_median": 0.48, "still_stoppable_mean": 0.4, "rings_total": 12,
                "rings_matching_an_attempt": 6,
                "per_shape": {"cycle": {"attempts": 4, "caught": 2, "shape_matched": 1, "shape_matched_final": 2}}}
    labels = {"rings": 12, "transfers_in_rings": 100, "laundering_in_rings": 74, "ring_transfer_precision": 0.74,
              "rings_mostly_laundering": 9, "rings_without_laundering": 2, "laundering_in_period": 150,
              "laundering_covered": 80, "laundering_covered_share": 80 / 150, "core_precision": None,
              "expanded_precision": None, "expanded_transfers": 0,
              "per_shape": {"cycle": {"rings": 3, "mean_precision": 0.56}}}
    sweep = [{"gnn_threshold": None, "ring_versions": 20, "rings": 12},
             {"gnn_threshold": 0.5, "ring_versions": 15, "rings": 9}]
    text = markdown_report({"attempts": attempts, "labels": labels, "gnn_sweep": sweep},
                           {"patterns": "p.txt", "since": "2022-09-08T16:12:00", "until": None})
    assert text.startswith("# Ring-level evaluation")
    assert "| patterns | `p.txt` |" in text and "until" not in text
    for line in ("| Known attempts starting in the period | 10 |", "| Caught by a ring | 6 (60%) |",
                 "| Covered (half the attempt's accounts in one ring) | 7 |",
                 "| Shape match rate at the first alert, among caught | 50% |",
                 "| Median minutes from first transfer to alert | 22 |",
                 "| Median share of the money still stoppable at the alert | 48% |",
                 "| Rings (latest versions) | 12 |", "| no GNN | 12 |", "| 0.50 | 9 |"):
        assert line in text
    only_labels = markdown_report({"labels": labels})
    assert "Headline numbers" not in only_labels and "GNN sweep" not in only_labels
    assert "No results" in markdown_report({})


def test_markdown_flag_writes_the_report_and_keeps_the_printed_output(tmp_path, capsys):
    from scripts.evaluate_rings import main
    path = tmp_path / "patterns.txt"
    path.write_text(PATTERNS)
    rings_file = tmp_path / "rings.jsonl"
    rings_file.write_text(json.dumps(ring("R-0001", 1, "scatter-gather", ["10_M0000001", "27_P0000001",
                                          "27_P0000002", "27_P0000003", "27_B0000001"], "2022-09-09T12:30:00")) + "\n")
    args = ["--patterns", str(path), "--rings-file", str(rings_file), "--since", "2022-09-08T16:12:00"]
    main(args)
    printed = capsys.readouterr().out
    md = tmp_path / "evaluation.md"
    main(args + ["--markdown", str(md)])
    assert capsys.readouterr().out == printed
    text = md.read_text()
    assert "| Known attempts starting in the period | 2 |" in text and "| ring versions read | `1` |" in text
