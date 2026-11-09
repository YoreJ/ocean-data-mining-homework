"""Audit stored pilot records only. No API client, execution, or network use."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import mean


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def field_names(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from field_names(item)
    elif isinstance(value, list):
        for item in value:
            yield from field_names(item)


def audit(root: Path, rules: Path) -> dict:
    requests = []
    aggregate = {}
    for ledger in sorted((root / "experiment/request_ledgers").glob("*.jsonl")):
        run = read_json(root / "experiment/runs" / (ledger.stem + ".json"))
        entries = [json.loads(line) for line in ledger.read_text().splitlines()]
        starts = [entry for entry in entries if entry["event"] == "attempt_started"]
        parsed = {entry["metadata"]["client_request_id"]: entry
                  for entry in entries if entry["event"] == "response_parsed"}
        for index, start in enumerate(starts):
            payload = start["payload"]
            reply = parsed[start["metadata"]["client_request_id"]]
            raw = reply["metadata"]["raw_usage"]
            event = run["events"][index]
            text = payload["messages"][0]["content"]
            observation = json.loads(text)
            history = observation["history"]
            decisions = observation["decisions"]
            duplicates = sum(any(decision["action"] == record["action"] for record in history)
                             for decision in decisions if decision["executed"])
            ordinary = raw["input_tokens"]
            creation = raw["cache_creation_input_tokens"]
            read = raw["cache_read_input_tokens"]
            total = ordinary + creation + read
            record_ids = [record["record_id"] for record in history]
            nested_creation = sum(raw.get("cache_creation", {}).values())
            all_names = set(field_names(payload))
            requests.append({
                "run_id": ledger.stem, "group": run["group"], "step": index + 1,
                "client_request_id": start["metadata"]["client_request_id"],
                "payload_keys": sorted(payload), "message_count": len(payload["messages"]),
                "system_chars": len(payload["system"]), "user_chars": len(text),
                "visible_text_chars": len(payload["system"]) + len(text),
                "history_count": len(history), "decision_count": len(decisions),
                "duplicate_history_record_ids": len(record_ids) != len(set(record_ids)),
                "executed_actions_repeated_in_decisions": duplicates,
                "contract_json_chars": len(json.dumps(observation["contract"], sort_keys=True)),
                "observation_field_chars": {key: len(json.dumps(value, sort_keys=True))
                                            for key, value in observation.items()},
                "forbidden_field_names": sorted(all_names & {
                    "case_id", "group", "schedules", "audit", "expected", "configuration", "checkpoint"}),
                "native_tools_supplied": "tools" in payload,
                "cache_control_supplied": "cache_control" in all_names,
                "raw_usage": raw, "input_total": total,
                "nested_cache_creation_matches_parent": nested_creation == creation,
                "event_usage_matches_recomputed": event["usage"]["input_tokens"] == total,
                "usage_matches_raw_http": read_json_usage(reply) == raw,
            })
    groups = {}
    runs_by_group = {group: [read_json(path) for path in sorted((root / "experiment/runs").glob(f"*-{group}.json"))]
                     for group in ("M", "MC")}
    runs_by_group["R"] = [read_json(path) for path in sorted(rules.glob("*.json"))]
    for group in ("R", "M", "MC"):
        runs = runs_by_group[group]
        events = [event for run in runs for event in run["events"]]
        reqs = [item for item in requests if item["group"] == group]
        action_counts = Counter(event["action"]["tool"] for event in events if event["action"])
        interceptable_queries = sum(
            event["action"] is not None and event["action"]["tool"] == "query_temperature"
            and (event.get("offline_check") or {}).get("decision") == "reject"
            for event in events)
        group_info = {
            "data_source": str(rules if group == "R" else root / "experiment/runs"),
            "cases": [{"case_id": run["case_id"], "outcome": run["outcome"], **run["totals"]} for run in runs],
            "outcomes": {outcome: sum(run["outcome"] == outcome for run in runs)
                         for outcome in sorted({run["outcome"] for run in runs})},
            "tool_calls": sum(run["totals"]["tool_calls"] for run in runs),
            "model_calls": sum(run["totals"]["model_calls"] for run in runs),
            "proposal_action_counts": dict(action_counts),
            "query_proposals": action_counts["query_temperature"],
            "interceptable_query_proposals": interceptable_queries,
            "checker_dispatch_calls": sum(event["check"]["enabled"] for event in events) if group == "MC" else None,
            "query_scope_checks": action_counts["query_temperature"] if group == "MC" else None,
            "recovery_elapsed_ms": sum(run["totals"]["elapsed_ms"] for run in runs),
            "model_adapter_elapsed_ms": sum(event["model_call_ms"] for event in events),
            "tool_elapsed_ms": sum(event["tool_ms"] for event in events),
            "checker_elapsed_ms": sum(event["check_ms"] for event in events) if group == "MC" else None,
            "rule_decision_elapsed_ms": None,
            "checker_measurements_ms": [event["check_ms"] for event in events] if group == "MC" else [],
            "raw_usage_totals": {key: sum(item["raw_usage"].get(key, 0) for item in reqs)
                                 for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")},
            "reported_input_total": sum(item["input_total"] for item in reqs),
            "mean_reported_input_per_request": mean(item["input_total"] for item in reqs) if reqs else None,
            "visible_text_chars_range": [min(item["visible_text_chars"] for item in reqs),
                                         max(item["visible_text_chars"] for item in reqs)] if reqs else None,
            "total_visible_text_chars": sum(item["visible_text_chars"] for item in reqs),
            "repeated_action_occurrences_in_decisions": sum(item["executed_actions_repeated_in_decisions"] for item in reqs),
            "fee": None,
        }
        if group in ("M", "MC"):
            group_info["summary_token_sum_matches"] = group_info["reported_input_total"] == sum(
                run["totals"]["tokens"]["input_tokens"] for run in runs)
        groups[group] = group_info
    case08 = {}
    for group in ("R", "M", "MC"):
        run = next(run for run in runs_by_group[group] if run["case_id"] == "08")
        case08[group] = {"outcome": run["outcome"], "termination": run["termination"],
                         "last_action": run["events"][-1]["action"],
                         "budgets_before_last_action": run["events"][-1]["budgets_before"],
                         "budgets_after_last_action": run["events"][-1]["budgets_after"],
                         "optimal_stopping_supported": False}
    manifest = read_json(root / "experiment/manifest.json")
    saved_summary = read_json(root / "experiment/summary.json")
    client_ids = [item["client_request_id"] for item in requests]
    report = {"kind": "offline_result_audit", "new_model_requests": 0,
              "source_manifest_sha256": hashlib.sha256((root / "experiment/manifest.json").read_bytes()).hexdigest(),
              "groups": groups, "requests": requests, "case08": case08,
              "checks": {
                  "request_count": len(requests),
                  "unique_client_request_ids": len(set(client_ids)) == len(client_ids),
                  "saved_summary_input_totals_match": all(
                      saved_summary["groups"][group]["tokens"]["input_tokens"] == groups[group]["reported_input_total"]
                      for group in ("M", "MC")),
                  "rule_checkpoints_match_model_checkpoints": all(
                      next(run for run in runs_by_group["R"] if run["case_id"] == case_id)["initial_checkpoint_hash"]
                      == next(run for run in runs_by_group[group] if run["case_id"] == case_id)["initial_checkpoint_hash"]
                      for case_id in (f"{index:02d}" for index in range(1, 9)) for group in ("M", "MC")),
                  "raw_http_usage_matches_all_ledger_usage": all(r["usage_matches_raw_http"] for r in requests),
                  "nested_cache_creation_matches_all_parent_fields": all(r["nested_cache_creation_matches_parent"] for r in requests),
                  "all_normalized_event_usage_matches": all(r["event_usage_matches_recomputed"] for r in requests),
                  "any_repeated_history_ids": any(r["duplicate_history_record_ids"] for r in requests),
                  "any_forbidden_fields": any(r["forbidden_field_names"] for r in requests),
                  "cache_control_sent_count": sum(r["cache_control_supplied"] for r in requests),
                  "unique_ordinary_input_counts": sorted({r["raw_usage"]["input_tokens"] for r in requests}),
                  "nonzero_cache_read_and_write_count": sum(r["raw_usage"]["cache_read_input_tokens"] > 0
                                                           and r["raw_usage"]["cache_creation_input_tokens"] > 0 for r in requests),
              }, "limitations": [
                  "Usage is gateway-reported; upstream prompt, tokenizer and cache implementation unverified.",
                  "R timing is from a separate earlier local run; not a paired timing benchmark.",
                  "Checker timing covers inspect_action wrapper only, excluding wider logging/feedback overhead.",
                  "No triggered intervention means reselection effects have no observed sample.",
              ], "source_experiment_status": manifest["status"]}
    return report


def read_json_usage(reply: dict) -> dict:
    return json.loads(reply["metadata"]["raw_http_response"])["usage"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("results/claude-pilot-20261109"))
    parser.add_argument("--rules", type=Path, default=Path("results/api-preparation-program-check/rule_baseline"))
    args = parser.parse_args()
    report = audit(args.root, args.rules)
    destination = args.root / "result-audit.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": report["checks"], "groups": {
        group: {key: data[key] for key in ("recovery_elapsed_ms", "model_adapter_elapsed_ms", "checker_elapsed_ms",
                                          "raw_usage_totals", "reported_input_total", "total_visible_text_chars")}
        for group, data in report["groups"].items()}}, ensure_ascii=False, indent=2))
