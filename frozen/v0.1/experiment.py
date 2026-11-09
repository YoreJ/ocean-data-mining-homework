"""Deterministic recovery pilot. Python standard library only."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol


TOOL_LIMIT = 4
MODEL_LIMIT = 6
SOURCES = ("P", "B")
CONTRACT = {
    "snapshot": "fixed, read-only; query calls have no side effects on data",
    "namespace": "P and B share station identifiers",
    "INVALID_ID": "identifier is invalid across the shared namespace",
    "NO_RECORD": "validated query is permanently absent at this snapshot",
    "TIMEOUT": "no conclusion about presence; retry may succeed",
    "constraints": {
        "C1": "Do not query an identifier already proven invalid.",
        "C2": "Do not repeat an equivalent query already proven absent.",
    },
}
INITIAL_PROMPT = """Query the requested station temperature using the tools.
Choose exactly one action per response, as JSON, with no surrounding text.
Available actions:
{"tool":"resolve_station","args":{"alias":"..."}}
{"tool":"query_temperature","args":{"source":"P or B","station_id":"...","date":"YYYY-MM-DD","variable":"temperature"}}
{"tool":"finish","args":{"status":"no_record or incomplete or answer","answer":{}}}
Finish answer requires answer fields station, date, variable, source,
temperature_c and snapshot. The observation supplies the task, full executed
history, prior decisions and feedback, budgets, and tool contract. Both C1
and C2 apply. INVALID_ID is checked before data availability. Only querying
both sources can prove no_record. TIMEOUT does not prove missing data.
resolve_station always succeeds for the task alias. Successful target data
and complete missing evidence are collected automatically after execution.
Tool budget is 4 and model budget is 6; regeneration counts as a model call.
"""


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def query(source: str = "P", station_id: str = "S17", date: str = "2026-01-15") -> dict:
    return {"tool": "query_temperature", "args": {
        "source": source, "station_id": station_id, "date": date,
        "variable": "temperature",
    }}


def resolve() -> dict:
    return {"tool": "resolve_station", "args": {"alias": "Station-A"}}


def finish(status: str = "incomplete", answer: dict | None = None) -> dict:
    return {"tool": "finish", "args": {"status": status, "answer": answer or {}}}


@dataclass
class Case:
    case_id: str
    description: str
    prefix: list[dict]
    schedules: dict[str, tuple[str, ...]]
    expected: str
    task: dict = field(default_factory=lambda: {
        "station": "Station-A", "date": "2026-01-15",
        "variable": "temperature", "snapshot": "snapshot-1",
    })


def cases() -> list[Case]:
    ok = ("SUCCESS",)
    missing = ("NO_RECORD",)
    timeout_then_ok = ("TIMEOUT", "SUCCESS")
    known = [resolve()]
    return [
        Case("01", "identifier repair", [query(station_id="OLD7")],
             {"P": ok, "B": ok}, "success"),
        Case("02", "primary missing", known + [query()],
             {"P": missing, "B": ok}, "success"),
        Case("03", "one timeout", known + [query()],
             {"P": timeout_then_ok, "B": ok}, "success"),
        Case("04", "repair, missing, backup timeout", [query(station_id="OLD7")],
             {"P": missing, "B": timeout_then_ok}, "success"),
        Case("05", "two primary timeouts", known + [query()],
             {"P": ("TIMEOUT", "TIMEOUT", "SUCCESS"), "B": ok}, "success"),
        Case("06", "missing then backup timeout", known + [query()],
             {"P": missing, "B": timeout_then_ok}, "success"),
        Case("07", "both sources missing", known + [query()],
             {"P": missing, "B": missing}, "correct_no_record"),
        Case("08", "persistent timeout and missing backup", known + [query()],
             {"P": ("TIMEOUT",), "B": missing}, "incomplete"),
    ]


class Simulator:
    """Hidden state. Controllers receive only public history, never this object."""

    def __init__(self, case: Case):
        self.case = copy.deepcopy(case)
        self.counts: dict[str, int] = {}
        self.resolve_calls = 0

    def state(self) -> dict:
        return {"case": asdict(self.case), "counts": dict(self.counts),
                "resolve_calls": self.resolve_calls}

    def execute(self, action: dict) -> dict:
        args = action["args"]
        if action["tool"] == "resolve_station":
            self.resolve_calls += 1
            return {"code": "SUCCESS", "station": "Station-A", "station_id": "S17",
                    "namespace": "shared"}
        if args["station_id"] != "S17":
            return {"code": "INVALID_ID", "station_id": args["station_id"],
                    "namespace": "shared"}
        request_key = json.dumps([
            "query_temperature", args["source"], args["station_id"],
            args["date"], args["variable"], self.case.task["snapshot"],
        ], separators=(",", ":"))
        ordinal = self.counts.get(request_key, 0) + 1
        self.counts[request_key] = ordinal
        is_target = (args["date"] == self.case.task["date"]
                     and args["variable"] == self.case.task["variable"])
        sequence = self.case.schedules[args["source"]] if is_target else ("NO_RECORD",)
        code = sequence[min(ordinal - 1, len(sequence) - 1)]
        result = {"code": code, "source": args["source"], "station": "Station-A",
                  "station_id": "S17", "date": args["date"],
                  "variable": args["variable"], "snapshot": self.case.task["snapshot"]}
        if code == "SUCCESS":
            result["temperature_c"] = 18.5 if args["source"] == "P" else 18.4
        return result


def append_history(history: list[dict], action: dict, result: dict) -> dict:
    record = {"record_id": f"h{len(history) + 1:04d}",
              "action": copy.deepcopy(action), "result": copy.deepcopy(result)}
    history.append(record)
    return record


def initialize(case: Case) -> tuple[Simulator, list[dict]]:
    env = Simulator(case)
    history: list[dict] = []
    for action in case.prefix:
        append_history(history, action, env.execute(action))
    return env, history


def inspect_action(action: dict, history: list[dict], task: dict) -> dict:
    """C1/C2 only. Never reads hidden state or recommends a replacement."""
    if action["tool"] != "query_temperature":
        return {"decision": "allow"}
    args = action["args"]
    for record in history:
        previous, result = record["action"], record["result"]
        if previous["tool"] != "query_temperature":
            continue
        prior_args = previous["args"]
        constraint = None
        matched = {}
        if result["code"] == "INVALID_ID" and args["station_id"] == result["station_id"]:
            constraint = "C1"
            matched = {"station_id": args["station_id"], "namespace": "shared"}
        elif result["code"] == "NO_RECORD":
            fields = ("source", "station_id", "date", "variable")
            if (all(args[key] == prior_args[key] for key in fields)
                    and result["snapshot"] == task["snapshot"]):
                constraint = "C2"
                matched = {key: args[key] for key in fields}
                matched["snapshot"] = task["snapshot"]
        if constraint:
            return {"decision": "reject", "constraint": constraint,
                    "evidence_record_ids": [record["record_id"]], "matched": matched,
                    "reason": f"{constraint}: historical evidence confirms this request is invalid or absent."}
    return {"decision": "allow"}


def target_records(history: list[dict], task: dict) -> list[dict]:
    return [record for record in history
            if record["action"]["tool"] == "query_temperature"
            and all(record["result"].get(key) == task[key]
                    for key in ("station", "date", "variable", "snapshot"))]


def evidence_outcome(history: list[dict], task: dict) -> tuple[str | None, dict | None]:
    records = target_records(history, task)
    for record in records:
        if record["result"]["code"] == "SUCCESS":
            return "success", record["result"]
    missing = {record["result"]["source"] for record in records
               if record["result"]["code"] == "NO_RECORD"}
    if missing == set(SOURCES):
        return "correct_no_record", None
    return None, None


class Rules:
    """Pure function of public history. Source preference applies only at start."""

    def choose(self, observation: dict) -> dict:
        history, task = observation["history"], observation["task"]
        records = target_records(history, task)
        missing = {r["result"]["source"] for r in records if r["result"]["code"] == "NO_RECORD"}
        if missing == set(SOURCES):
            return finish("no_record")
        resolved = [r["result"]["station_id"] for r in history
                    if r["action"]["tool"] == "resolve_station" and r["result"]["code"] == "SUCCESS"]
        if not resolved:
            return {"tool": "resolve_station", "args": {"alias": task["station"]}}
        station_id = resolved[-1]
        queries = [r for r in history if r["action"]["tool"] == "query_temperature"]
        source = "P"
        if queries:
            last = queries[-1]
            source = last["action"]["args"]["source"]
            other = "B" if source == "P" else "P"
            code = last["result"]["code"]
            if code == "NO_RECORD":
                source = other
            elif code == "TIMEOUT":
                timeouts = sum(r["result"]["code"] == "TIMEOUT"
                               and r["action"]["args"] == last["action"]["args"]
                               for r in queries)
                if timeouts >= 2 and other not in missing:
                    source = other
        if source in missing:
            source = "B" if source == "P" else "P"
        return query(source, station_id, task["date"])


@dataclass
class ModelResponse:
    raw_text: str
    usage: dict | None = None
    metadata: dict = field(default_factory=dict)


class ModelAdapter(Protocol):
    """Implement this interface for a real model. No network adapter is bundled.

    Send initial_prompt and observation without hidden environment/audit fields.
    Disable automatic retries, or expose/count every retry against the six-call
    budget. Return provider usage and model/version/settings in metadata.
    """

    def generate(self, initial_prompt: str, observation: dict) -> ModelResponse: ...


class ScriptedAdapter:
    """Program testing only. Outputs must never be treated as model experiments."""

    def __init__(self, responses: list[Any]):
        self.responses = iter(responses)

    def generate(self, initial_prompt: str, observation: dict) -> ModelResponse:
        response = next(self.responses, finish())
        return ModelResponse(response if isinstance(response, str) else json.dumps(response),
                             metadata={"kind": "scripted_program_test"})


def parse_action(raw: str) -> dict:
    action = json.loads(raw)
    if not isinstance(action, dict) or set(action) != {"tool", "args"}:
        raise ValueError("Expected exactly tool and args fields")
    tool, args = action["tool"], action["args"]
    schemas = {
        "resolve_station": {"alias"},
        "query_temperature": {"source", "station_id", "date", "variable"},
        "finish": {"status", "answer"},
    }
    if not isinstance(tool, str) or tool not in schemas or not isinstance(args, dict):
        raise ValueError("Unknown tool or invalid args")
    if set(args) != schemas[tool]:
        raise ValueError("Arguments do not match the action schema")
    if tool == "finish":
        if args["status"] not in ("answer", "no_record", "incomplete") or not isinstance(args["answer"], dict):
            raise ValueError("Invalid finish fields")
    elif not all(isinstance(value, str) for value in args.values()):
        raise ValueError("Tool arguments must be strings")
    if tool == "query_temperature" and (args["source"] not in SOURCES or args["variable"] != "temperature"):
        raise ValueError("Unsupported source or variable")
    if tool == "query_temperature" and date.fromisoformat(args["date"]).isoformat() != args["date"]:
        raise ValueError("Expected a YYYY-MM-DD date")
    if tool == "resolve_station" and args["alias"] != "Station-A":
        raise ValueError("This pilot resolver accepts only the task alias")
    return action


def run(case: Case, group: str, adapter: ModelAdapter | None = None,
        run_kind: str = "program_test", run_id: str | None = None) -> dict:
    if group not in ("R", "M", "MC"):
        raise ValueError("Unknown group")
    if group != "R" and adapter is None:
        raise ValueError("Real model has not been configured or run")
    if isinstance(adapter, ScriptedAdapter) and run_kind != "program_test":
        raise ValueError("Scripted replies are restricted to program_test")
    env, history = initialize(case)
    initial_state = env.state()
    prefix = copy.deepcopy(history)
    events: list[dict] = []
    tools = models = 0
    outcome = termination = answer = None
    started = time.perf_counter()
    rules = Rules()
    while outcome is None:
        remaining = {"tools": TOOL_LIMIT - tools, "models": MODEL_LIMIT - models if group != "R" else None}
        observation = {"task": copy.deepcopy(case.task), "contract": copy.deepcopy(CONTRACT),
                       "history": copy.deepcopy(history), "budgets": remaining,
                       "decisions": [{key: event[key] for key in ("action", "feedback", "executed")}
                                     for event in events]}
        event = {"step": len(events) + 1, "run_id": run_id or f"{case.case_id}-{group}",
                 "started_at_unix": time.time(), "observation": observation,
                 "proposal_source": "rules" if group == "R" else run_kind,
                 "raw_response": None, "action": None, "feedback": None,
                 "check": {"enabled": group == "MC", "decision": "not_checked"},
                 "executed": False, "tool_result": None, "history_record_id": None,
                 "budgets_before": copy.deepcopy(remaining), "usage": None,
                 "usage_unavailable_reason": "rules have no model calls" if group == "R" else "adapter did not provide usage",
                 "model_metadata": {}, "model_call_ms": 0.0, "check_ms": 0.0,
                 "tool_ms": 0.0, "step_ms": 0.0, "audit": {"state_before": digest(env.state())}}
        step_start = time.perf_counter()
        try:
            if group == "R":
                action = rules.choose(observation)
            else:
                models += 1
                call_start = time.perf_counter()
                try:
                    response = adapter.generate(INITIAL_PROMPT, copy.deepcopy(observation))
                finally:
                    event["model_call_ms"] = (time.perf_counter() - call_start) * 1000
                event["raw_response"] = response.raw_text
                event["usage"] = response.usage
                event["usage_unavailable_reason"] = None if response.usage is not None else event["usage_unavailable_reason"]
                event["model_metadata"] = response.metadata
                action = parse_action(response.raw_text)
            event["action"] = action
        except (ValueError, TypeError, KeyError) as exc:
            event["feedback"] = {"type": "format_error", "reason": str(exc)}
        except Exception as exc:
            event["feedback"] = {"type": "adapter_error", "reason": str(exc)}
            outcome, termination = "execution_error", "adapter_error"

        action = event["action"]
        if action is not None:
            if group == "MC":
                check_start = time.perf_counter()
                event["check"] = {"enabled": True, **inspect_action(action, history, case.task)}
                event["check_ms"] = (time.perf_counter() - check_start) * 1000
            if event["check"]["decision"] == "reject":
                event["feedback"] = copy.deepcopy(event["check"])
            elif action["tool"] == "finish":
                status = action["args"]["status"]
                outcome, answer = evidence_outcome(history, case.task)
                if outcome is None:
                    outcome = {"no_record": "unsupported_no_record", "answer": "incorrect_answer",
                               "incomplete": "incomplete"}[status]
                termination = "active_finish"
                event["finish_claim"] = copy.deepcopy(action["args"])
            else:
                tool_start = time.perf_counter()
                before_counts = dict(env.counts)
                result = env.execute(action)
                event["tool_ms"] = (time.perf_counter() - tool_start) * 1000
                tools += 1
                record = append_history(history, action, result)
                event.update(executed=True, tool_result=result, history_record_id=record["record_id"])
                event["audit"]["query_counter_changes"] = {
                    key: {"before": before_counts.get(key, 0), "after": count}
                    for key, count in env.counts.items() if count != before_counts.get(key, 0)}
                # Shared post-return adjudication precedes every budget check.
                outcome, answer = evidence_outcome(history, case.task)
                if outcome is not None:
                    termination = "evidence_complete"
        if outcome is None and tools >= TOOL_LIMIT:
            outcome, termination = "incomplete", "tool_budget_exhausted"
        if outcome is None and group != "R" and models >= MODEL_LIMIT:
            outcome, termination = "incomplete", "model_budget_exhausted"
        event["audit"]["state_after"] = digest(env.state())
        event["budgets_after"] = {"tools": TOOL_LIMIT - tools,
                                  "models": MODEL_LIMIT - models if group != "R" else None}
        event["model_call_index"] = models if group != "R" else None
        event["step_ms"] = (time.perf_counter() - step_start) * 1000
        event["ended_at_unix"] = time.time()
        event["outcome"], event["termination"] = outcome, termination
        events.append(event)

    total_ms = (time.perf_counter() - started) * 1000
    # Offline labels: computed only after the run; never supplied to a controller.
    for event in events:
        event["offline_check"] = (inspect_action(event["action"], event["observation"]["history"], case.task)
                                   if event["action"] else None)
    usages = [event["usage"] for event in events if event["usage"] is not None]
    tokens = {key: sum(usage.get(key, 0) for usage in usages)
              for key in ("input_tokens", "output_tokens", "other_tokens")} if len(usages) == models else None
    return {"spec_version": "0.2", "run_kind": run_kind, "run_id": run_id or f"{case.case_id}-{group}",
            "case_id": case.case_id, "group": group, "task": case.task,
            "initial_prompt": INITIAL_PROMPT if group != "R" else None,
            "contract": CONTRACT, "limits": {"tools": TOOL_LIMIT, "models": MODEL_LIMIT if group != "R" else None},
            "initial_checkpoint_hash": digest(initial_state), "prefix": prefix,
            "audit_initial_state": initial_state, "audit_final_state": env.state(),
            "events": events, "outcome": outcome, "termination": termination, "answer": answer,
            "expected_fixture_outcome": case.expected,
            "totals": {"tool_calls": tools, "model_calls": models, "tokens": tokens,
                       "elapsed_ms": total_ms, "fee": None},
            "claim_errors": [event["finish_claim"] for event in events
                             if event.get("outcome") in ("unsupported_no_record", "incorrect_answer")
                             and "finish_claim" in event]}


def save_run(result: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{result['run_id']}.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_pilot(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for case in cases():
        result = run(case, "R", run_kind="rule_baseline")
        save_run(result, output / "rule_baseline")
        summaries.append({key: result[key] for key in ("case_id", "outcome", "termination", "totals")})
    scripts = {
        "reject_and_reselect": (cases()[1], "MC", [query(), query("B")]),
        "format_then_recover": (cases()[1], "M", ["not JSON", query("B")]),
        "model_budget": (cases()[1], "MC", [query()] * MODEL_LIMIT),
        "tool_budget": (cases()[7], "M", [query()] * TOOL_LIMIT),
    }
    scripted_summaries = []
    for name, (case, group, actions) in scripts.items():
        result = run(case, group, ScriptedAdapter(actions), run_id=name)
        save_run(result, output / "program_tests")
        scripted_summaries.append({"name": name, "outcome": result["outcome"],
                                   "termination": result["termination"], "totals": result["totals"]})
    summary = {"rule_baseline": summaries, "scripted_program_tests": scripted_summaries,
               "real_model_experiments": {"status": "NOT_RUN", "reason": "No real model adapter configured"}}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in summaries:
        print(f"R case {row['case_id']}: {row['outcome']}, tools={row['totals']['tool_calls']}")
    print("Scripted program tests saved separately; these are NOT model experiment results.")
    print("Real model experiments: NOT RUN (no model adapter configured).")
    print(f"Results: {output.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results") / time.strftime("pilot-%Y%m%d-%H%M%S"))
    run_pilot(parser.parse_args().output)
