"""Run a paired, offline check of four recovery filters on fixed proposals."""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from unittest.mock import patch

import experiment


REPEAT_COUNTS = range(5)
TIMEOUT_COUNTS = range(3)
HARD_FAILURES = {"INVALID_ID", "NO_RECORD"}
DEFAULT_OUTPUT = Path(__file__).with_name("cases") / "recovery-benchmark.json"


@dataclass(frozen=True)
class Proposal:
    action: dict
    role: str = "fresh"
    expected_error: str | None = None


def exact_filter(errors: set[str]):
    """Reject an identical query only when its earlier response is in errors."""
    def check(action: dict, history: list[dict], _task: dict) -> dict:
        if action["tool"] == "query_temperature":
            for record in history:
                if record["action"] == action and record["result"]["code"] in errors:
                    return {"decision": "reject", "evidence_record_ids": [record["record_id"]]}
        return {"decision": "allow"}

    return check


def stress_cases():
    for family, repeats, timeouts in product(
        ("missing_record", "invalid_id"), REPEAT_COUNTS, TIMEOUT_COUNTS
    ):
        schedule = ("TIMEOUT",) * timeouts + ("SUCCESS",)
        case_id = f"{family}-{repeats}-{timeouts}"
        if family == "missing_record":
            prefix = [experiment.resolve(), experiment.query()]
            schedules = {"P": ("NO_RECORD",), "B": schedule}
            proposals = [Proposal(experiment.query(), "redundant", "NO_RECORD")] * repeats
        else:
            prefix = [experiment.query(station_id="OLD7")]
            schedules = {"P": schedule, "B": schedule}
            # OLD7 was already rejected by P. Alternate B and P to distinguish
            # invalid-ID evidence from exact argument matching.
            proposals = [Proposal(experiment.query(source, "OLD7"), "redundant", "INVALID_ID")
                         for source in ("B", "P", "B", "P")[:repeats]]
            proposals.append(Proposal(experiment.resolve()))
        proposals.append(Proposal(experiment.query("B")))
        proposals.extend(Proposal(experiment.query("B"), "timeout_retry") for _ in range(timeouts))
        case = experiment.Case(case_id, family, prefix, schedules, "success")
        yield case, proposals, {"family": family, "repeats": repeats, "timeouts": timeouts}


def controls():
    cases = {case.case_id: case for case in experiment.cases()}
    missing = cases["07"]
    yield missing, [Proposal(experiment.query(), "redundant", "NO_RECORD"),
                    Proposal(experiment.query("B"))], "both_sources_missing"
    timeout = cases["08"]
    yield timeout, [Proposal(experiment.query("B")),
                    Proposal(experiment.query(), "timeout_retry"),
                    Proposal(experiment.query(), "timeout_retry")], "persistent_timeout"
    other_date = experiment.Case(
        "different-date", "same source and station, different date",
        [experiment.resolve(), experiment.query(date="2026-01-14")],
        {"P": ("SUCCESS",), "B": ("SUCCESS",)}, "success",
    )
    yield other_date, [Proposal(experiment.query())], "different_date"
    exhausted = cases["02"]
    yield exhausted, ([Proposal(experiment.query(), "redundant", "NO_RECORD")]
                      * experiment.MODEL_LIMIT + [Proposal(experiment.query("B"))]), "proposal_limit"


def audit_trace(case: experiment.Case, proposals: list[Proposal], result: dict) -> dict:
    """Verify proposal labels and rejections against an independent tool replay."""
    simulator, prefix = experiment.initialize(case)
    assert result["prefix"] == prefix
    assert result["initial_checkpoint_hash"] == experiment.digest(simulator.state())
    trace = []
    for index, event in enumerate(result["events"]):
        proposal = proposals[index]
        assert event["action"] == proposal.action
        assert event["audit"]["state_before"] == experiment.digest(simulator.state())
        rejected = event["check"]["decision"] == "reject"
        row = {"proposal": index + 1, "role": proposal.role, "check": event["check"]["decision"]}
        if rejected:
            assert not event["executed"]
            assert event["budgets_before"]["tools"] == event["budgets_after"]["tools"]
            evidence_ids = event["check"]["evidence_record_ids"]
            assert evidence_ids and set(evidence_ids) <= {
                record["record_id"] for record in event["observation"]["history"]
            }
            row["evidence_record_ids"] = evidence_ids
            # Probe a clone: the tool itself determines the response from the
            # pending state without consulting either filter or the label.
            code = copy.deepcopy(simulator).execute(proposal.action)["code"]
            row["rejected_tool_code"] = code
        elif event["executed"]:
            assert event["budgets_before"]["tools"] - event["budgets_after"]["tools"] == 1
            tool_result = simulator.execute(proposal.action)
            assert event["tool_result"] == tool_result
            code = tool_result["code"]
            row["tool_code"] = code
        else:
            assert proposal.action["tool"] == "finish"
            code = None
        if proposal.expected_error is not None:
            assert proposal.expected_error == code
        assert event["audit"]["state_after"] == experiment.digest(simulator.state())
        assert event["budgets_before"]["models"] - event["budgets_after"]["models"] == 1
        trace.append(row)

    return {
        "outcome": result["outcome"],
        "termination": result["termination"],
        "tools": result["totals"]["tool_calls"],
        "proposals": result["totals"]["model_calls"],
        "redundant_executed": sum(row["role"] == "redundant" and "tool_code" in row for row in trace),
        "rejected": sum("rejected_tool_code" in row for row in trace),
        "retry_rejected": sum(row["role"] == "timeout_retry" and "rejected_tool_code" in row for row in trace),
        "successful_query_rejected": sum(row.get("rejected_tool_code") == "SUCCESS" for row in trace),
        "trace": trace,
    }


def compare(case: experiment.Case, proposals: list[Proposal]) -> dict:
    proposals = [*proposals, Proposal(experiment.finish(), "finish")]
    strategies = {
        "direct": None,
        "all_failure_exact": exact_filter(HARD_FAILURES | {"TIMEOUT"}),
        "hard_failure_exact": exact_filter(HARD_FAILURES),
        "scoped_evidence": experiment.inspect_action,
    }
    results = {}
    checkpoints = set()
    for name, checker in strategies.items():
        adapter = experiment.ScriptedAdapter([proposal.action for proposal in proposals])
        if checker is None:
            result = experiment.run(case, "M", adapter)
        else:
            with patch.object(experiment, "inspect_action", checker):
                result = experiment.run(case, "MC", adapter)
        checkpoints.add(result["initial_checkpoint_hash"])
        results[name] = audit_trace(case, proposals, result)
    assert len(checkpoints) == 1
    scoped = results["scoped_evidence"]
    assert scoped["redundant_executed"] == scoped["retry_rejected"] == 0
    assert all(row["role"] == "redundant" for row in scoped["trace"] if "rejected_tool_code" in row)
    return {
        "checkpoint_sha256": next(iter(checkpoints)),
        "prefix": result["prefix"],
        "schedules": case.schedules,
        "proposal_sequence": [proposal.action for proposal in proposals],
        "strategies": results,
    }


def summarize(rows: list[dict]) -> dict:
    result = {}
    counts = ("tools", "proposals", "redundant_executed", "rejected", "retry_rejected", "successful_query_rejected")
    for strategy in rows[0]["strategies"]:
        runs = [row["strategies"][strategy] for row in rows]
        result[strategy] = {"success": sum(run["outcome"] == "success" for run in runs)}
        result[strategy].update({key: sum(run[key] for run in runs) for key in counts})
    return result


def benchmark() -> dict:
    rows = []
    rule_reference = []
    for case, proposals, factors in stress_cases():
        feasible = len(proposals) <= experiment.MODEL_LIMIT
        row = {"case": case.case_id, **factors, "fits_proposal_budget": feasible, **compare(case, proposals)}
        assert (row["strategies"]["scoped_evidence"]["outcome"] == "success") == feasible
        rows.append(row)
        # Only one reference per unique starting environment. The adaptive rule
        # baseline supplies its own actions; it does not receive injected errors.
        if factors["repeats"] == 0:
            result = experiment.run(case, "R", run_kind="rule_baseline")
            rule_reference.append({"case": case.case_id, "outcome": result["outcome"],
                                   "tools": result["totals"]["tool_calls"]})

    control_rows = [{"control": name, **compare(case, proposals)}
                    for case, proposals, name in controls()]
    expected = {"both_sources_missing": "correct_no_record", "persistent_timeout": "incomplete",
                "different_date": "success", "proposal_limit": "incomplete"}
    for row in control_rows:
        assert row["strategies"]["scoped_evidence"]["outcome"] == expected[row["control"]]
    return {
        "kind": "synthetic_paired_recovery_benchmark",
        "protocol": {"tool_limit": experiment.TOOL_LIMIT, "proposal_limit": experiment.MODEL_LIMIT,
                     "repeat_counts": list(REPEAT_COUNTS), "timeout_counts": list(TIMEOUT_COUNTS),
                     "checkpoint_calls_excluded": True, "network_model_calls": 0},
        "stress_case_count": len(rows),
        "stress_cases_fitting_proposal_budget": sum(row["fits_proposal_budget"] for row in rows),
        "summary": summarize(rows),
        "summary_by_family": {family: summarize([row for row in rows if row["family"] == family])
                              for family in ("missing_record", "invalid_id")},
        "stress_cases": rows,
        "controls": control_rows,
        "adaptive_rules_reference_without_injected_proposals": rule_reference,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Destination for deterministic JSON")
    args = parser.parse_args()
    args.output.write_text(json.dumps(benchmark(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
