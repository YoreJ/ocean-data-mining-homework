"""Explicit, no-retry connectivity check and sixteen-run real-model pilot."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from experiment import (
    CONTRACT, INITIAL_PROMPT, MODEL_LIMIT, TOOL_LIMIT, ModelInterfaceError,
    cases, digest, initialize, parse_action, run, save_run,
)
from model_connection import JsonAPIAdapter, execution_plan, validate_config, verify_frozen


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    issues = validate_config(config)
    frozen = verify_frozen()
    if not frozen["snapshots_intact"] or not frozen["behavior_components_unchanged"]:
        issues.append("Frozen behavior differs from v0.1")
    if issues:
        raise ValueError("; ".join(issues))
    return config


def connectivity(config: dict, output: Path, name: str) -> dict:
    directory = output / "connectivity" / name
    directory.mkdir(parents=True, exist_ok=False)
    case = cases()[2]
    _, history = initialize(case)
    observation = {"task": case.task, "contract": CONTRACT, "history": history,
                   "budgets": {"tools": TOOL_LIMIT, "models": MODEL_LIMIT}, "decisions": []}
    report = {"kind": "connectivity_only", "included_in_experiment": False,
              "config": config, "config_hash": digest(config), "prompt_hash": digest(INITIAL_PROMPT),
              "started_at_unix": time.time(), "observation": observation,
              "status": "STARTED", "model_requests": 1, "usage": None}
    write_json(directory / "result.json", report)
    adapter = JsonAPIAdapter(config, directory / "requests.jsonl")
    try:
        response = adapter.generate(INITIAL_PROMPT, observation)
        report.update(raw_response=response.raw_text, usage=response.usage, metadata=response.metadata)
        try:
            report["action"] = parse_action(response.raw_text)
            report["status"] = "PASSED"
        except (ValueError, KeyError, TypeError) as exc:
            report.update(status="MODEL_FORMAT_ERROR", format_error=str(exc))
    except ModelInterfaceError as exc:
        report.update(status="INTERFACE_ERROR", error=str(exc), metadata=exc.metadata, usage=exc.usage)
    report["ended_at_unix"] = time.time()
    write_json(directory / "result.json", report)
    print(json.dumps({"connectivity": name, "status": report["status"], "usage": report["usage"],
                      "requested_model": config["model"],
                      "returned_model": report.get("metadata", {}).get("returned_model")}, ensure_ascii=False), flush=True)
    return report


def summarize(results: list[dict]) -> dict:
    rows = []
    for result in results:
        eligible = sum(event.get("offline_check", {}).get("decision") == "reject"
                       for event in result["events"] if event.get("offline_check"))
        rejected = [event for event in result["events"] if event["check"]["decision"] == "reject"]
        progress = []
        for rejection in rejected:
            later = [event for event in result["events"] if event["step"] > rejection["step"] and event["executed"]]
            progress.append({"rejected_step": rejection["step"],
                             "later_execution_steps": [event["step"] for event in later],
                             "later_success_or_new_missing_evidence": any(
                                 event["tool_result"]["code"] in ("SUCCESS", "NO_RECORD") for event in later),
                             "final_outcome": result["outcome"]})
        rows.append({"run_id": result["run_id"], "case_id": result["case_id"], "group": result["group"],
                     "actual_order": result["actual_order"], "outcome": result["outcome"],
                     "termination": result["termination"], "valid": result["valid_for_task_statistics"],
                     "proposals": len(result["events"]), "interceptable_actions": eligible,
                     "actual_interceptions": len(rejected), "post_interception": progress,
                     "format_errors": sum((e["feedback"] or {}).get("type") == "format_error" for e in result["events"]),
                     "interface_errors": sum((e["feedback"] or {}).get("type") == "interface_error" for e in result["events"]),
                     **result["totals"]})
    groups = {}
    for group in ("M", "MC"):
        subset = [row for row in rows if row["group"] == group]
        tokens_known = all(row["tokens"] is not None for row in subset)
        groups[group] = {
            "tasks": len(subset), "outcomes": {outcome: sum(row["outcome"] == outcome for row in subset)
                                               for outcome in sorted({row["outcome"] for row in subset})},
            **{key: sum(row[key] for row in subset) for key in (
                "tool_calls", "model_calls", "interceptable_actions", "actual_interceptions", "format_errors", "interface_errors")},
            "tokens": {key: sum(row["tokens"][key] for row in subset)
                       for key in ("input_tokens", "output_tokens", "other_tokens")} if tokens_known else None,
            "elapsed_ms": sum(row["elapsed_ms"] for row in subset), "fee": None,
        }
    return {"rows": rows, "groups": groups, "fee": None,
            "limitations": ["Third-party gateway; backend model identity independently unverified",
                            "Eight fixed tasks and one run per model group; descriptive pilot only",
                            "Currency amount unknown; provider usage retained as reported"]}


def batch(config: dict, output: Path, probe_name: str) -> dict:
    probe = json.loads((output / "connectivity" / probe_name / "result.json").read_text())
    if (probe["status"] != "PASSED" or probe["config_hash"] != digest(config)
            or probe["prompt_hash"] != digest(INITIAL_PROMPT)):
        raise ValueError("Successful connectivity check must match exact config and prompt")
    directory = output / "experiment"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "model_prompt.txt").write_text(INITIAL_PROMPT, encoding="utf-8")
    plan = execution_plan()
    manifest = {"kind": "real_model_experiment", "status": "RUNNING", "config": config,
                "prompt_hash": digest(INITIAL_PROMPT), "frozen": verify_frozen(), "plan": plan,
                "connectivity_reference": probe_name, "max_model_requests": 96,
                "started_at_unix": time.time(), "fee": None}
    write_json(directory / "manifest.json", manifest)
    results = []
    case_map = {case.case_id: case for case in cases()}
    try:
        for entry in plan:
            entry.update(status="RUNNING", actual_started_at=time.time(), actual_order=len(results) + 1)
            write_json(directory / "manifest.json", manifest)
            # Same adapter class and configuration; each request is stateless.
            adapter = JsonAPIAdapter(config, directory / "request_ledgers" / f"{entry['run_id']}.jsonl")
            result = run(case_map[entry["case_id"]], entry["group"], adapter,
                         run_kind="real_model", run_id=entry["run_id"])
            result.update(actual_order=entry["actual_order"], configuration=config,
                          actual_started_at=entry["actual_started_at"], actual_ended_at=time.time())
            save_run(result, directory / "runs")
            results.append(result)
            entry.update(status="COMPLETED" if result["valid_for_task_statistics"] else "INVALID_INTERFACE_ERROR",
                         actual_ended_at=result["actual_ended_at"])
            write_json(directory / "summary.json", summarize(results))
            write_json(directory / "manifest.json", manifest)
            print(json.dumps({"order": entry["actual_order"], "run_id": entry["run_id"],
                              "outcome": result["outcome"], **result["totals"]}, ensure_ascii=False), flush=True)
            if not result["valid_for_task_statistics"]:
                manifest["status"] = "STOPPED_INTERFACE_ERROR"
                break
        else:
            manifest["status"] = "COMPLETED"
    except BaseException:
        manifest["status"] = "INTERRUPTED_REQUIRES_LEDGER_REVIEW"
        write_json(directory / "manifest.json", manifest)
        raise
    manifest["ended_at_unix"] = time.time()
    write_json(directory / "manifest.json", manifest)
    return summarize(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("connectivity", "batch"))
    parser.add_argument("--config", type=Path, default=Path("model_config.local.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe", default="attempt-01")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.phase == "connectivity":
        connectivity(config, args.output, args.probe)
    else:
        batch(config, args.output, args.probe)
