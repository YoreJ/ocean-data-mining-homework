"""Evaluate version-scoped query checks against an independent tool simulator."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from itertools import product
from pathlib import Path

import experiment
from versioned_recovery import Context, Query, Record, Rejection, rejection_for


OUTPUT = Path(__file__).with_name("cases") / "versioned-recovery-benchmark.json"
PRIMARY, BACKUP = "P", "B"
DAY = "2026-01-15"
P_STATION = Query(PRIMARY, "S17")
B_STATION = Query(BACKUP, "S17")
P_OLD = Query(PRIMARY, "OLD7")
B_OLD = Query(BACKUP, "OLD7")
HARD_FAILURES = {"INVALID_ID", "NO_RECORD"}
ARMS = ("direct", "hard_exact", "published_static", "global_flush", "version_scoped")

# Registry contents and observations are defined independently of every gate.
# The same snapshot can contain a row for the physical station that only a
# newer registry maps to a particular identifier.
REGISTRIES = {
    ("regional", "r1"): {"S17": "station-a", "S22": "station-b"},
    ("regional", "r2"): {"S17": "station-a", "S22": "station-b", "OLD7": "station-a"},
    ("regional", "r3"): {"S17": "station-b", "S22": "station-a"},
    ("backup-native", "r1"): {"S17": "station-a", "OLD7": "station-a"},
}
SNAPSHOTS = {
    (PRIMARY, "s1"): {("station-b", DAY, "temperature"): 17.2},
    (PRIMARY, "s2"): {("station-a", DAY, "temperature"): 18.5,
                      ("station-b", DAY, "temperature"): 17.2},
    (BACKUP, "s1"): {("station-a", DAY, "temperature"): 18.4,
                     ("station-b", DAY, "temperature"): 17.1},
}


@dataclass(frozen=True)
class View:
    primary: Context = Context("regional", "r1", "s1")
    backup: Context = Context("regional", "r1", "s1")

    def source(self, name: str) -> Context:
        return {PRIMARY: self.primary, BACKUP: self.backup}[name]

    def disclose(self, source: str, missing: tuple[str, ...]) -> View:
        context = replace(self.source(source), **{field: None for field in missing})
        field = {PRIMARY: "primary", BACKUP: "backup"}[source]
        return replace(self, **{field: context})


@dataclass(frozen=True)
class Fixture:
    name: str
    stratum: str
    before: View
    current: View
    history_query: Query
    history_code: str
    candidate: Query
    target: Query
    # The fixture declares whether the candidate is known redundant, or has
    # incomplete public evidence. Neither label is ever passed into a gate.
    redundant: int = 0
    timeouts: int = 0
    history_missing: tuple[str, ...] = ()
    current_missing: tuple[str, ...] = ()
    unknown_probe: bool = False


DEFAULT = View()
DATA_UPDATE = View(primary=Context("regional", "r1", "s2"))
REGISTRY_UPDATE = View(primary=Context("regional", "r2", "s2"))
REMAP = View(primary=Context("regional", "r3", "s1"))
INDEPENDENT_BACKUP = View(backup=Context("backup-native", "r1", "s1"))
BACKUP_NEW_REVISION = View(backup=Context("regional", "r2", "s1"))


def fixtures():
    for retries in (0, 1):
        for name, before, current, anchor, code, target in (
            ("late_data", DEFAULT, DATA_UPDATE, P_STATION, "NO_RECORD", P_STATION),
            ("id_valid_after_revision", DATA_UPDATE, REGISTRY_UPDATE, P_OLD, "INVALID_ID", P_OLD),
            ("different_namespace", INDEPENDENT_BACKUP, INDEPENDENT_BACKUP, P_OLD, "INVALID_ID", B_OLD),
            ("different_source_revision", BACKUP_NEW_REVISION, BACKUP_NEW_REVISION, P_OLD, "INVALID_ID", B_OLD),
            ("registry_remap_same_snapshot", DEFAULT, REMAP, P_STATION, "NO_RECORD", P_STATION),
        ):
            yield Fixture(name, "stale", before, current, anchor, code, target, target, timeouts=retries)

    for repeats, retries in product((1, 3), (0, 1)):
        for name, current, anchor, code, candidate in (
            ("stable_missing", DEFAULT, P_STATION, "NO_RECORD", P_STATION),
            ("shared_registry_invalid", DEFAULT, P_OLD, "INVALID_ID", B_OLD),
            ("data_update_preserves_invalid", DATA_UPDATE, P_OLD, "INVALID_ID", P_OLD),
        ):
            yield Fixture(name, "retained", DEFAULT, current, anchor, code, candidate, B_STATION,
                          redundant=repeats, timeouts=retries)

    yield Fixture("timeout_retry", "control", DEFAULT, DEFAULT, B_STATION, "TIMEOUT",
                  B_STATION, B_STATION, timeouts=1)
    yield Fixture("missing_is_local_to_source", "control", DEFAULT, DEFAULT, P_STATION, "NO_RECORD",
                  B_STATION, B_STATION)
    different_date = Query(PRIMARY, "S22", "2026-01-14")
    date_target = Query(PRIMARY, "S22")
    yield Fixture("missing_is_local_to_date", "control", DEFAULT, DEFAULT, different_date, "NO_RECORD",
                  date_target, date_target)
    yield Fixture("missing_current_namespace", "control", INDEPENDENT_BACKUP, INDEPENDENT_BACKUP,
                  P_OLD, "INVALID_ID", B_OLD, B_OLD, current_missing=("namespace",))
    yield Fixture("missing_history_revision", "control", DATA_UPDATE, REGISTRY_UPDATE,
                  P_OLD, "INVALID_ID", P_OLD, P_OLD, history_missing=("registry_revision",))
    yield Fixture("missing_both_snapshots", "control", DEFAULT, DATA_UPDATE, P_STATION, "NO_RECORD",
                  P_STATION, P_STATION, history_missing=("data_snapshot",), current_missing=("data_snapshot",))
    yield Fixture("invalid_survives_unknown_snapshot", "control", DEFAULT, DATA_UPDATE,
                  P_OLD, "INVALID_ID", P_OLD, B_STATION, redundant=1, current_missing=("data_snapshot",))
    # Identical observable current metadata can hide either registry version.
    # Both probes must be allowed; an executed unknown failure is not grouped
    # with avoidable known-hard errors.
    for name, current in (("unknown_revision_now_valid", REGISTRY_UPDATE),
                          ("unknown_revision_still_invalid", DATA_UPDATE)):
        yield Fixture(name, "control", DATA_UPDATE, current, P_OLD, "INVALID_ID", P_OLD, B_STATION,
                      current_missing=("registry_revision",), unknown_probe=True)


class Simulator:
    def __init__(self, target: Query, timeouts: int):
        self.target = target
        self.timeouts = timeouts
        self.attempts = Counter()

    def inspect(self, query: Query, context: Context):
        station = REGISTRIES[context.namespace, context.registry_revision].get(query.station_id)
        if station is None:
            return "INVALID_ID", None
        temperature = SNAPSHOTS[query.source, context.data_snapshot].get((station, query.date, query.variable))
        if temperature is None:
            return "NO_RECORD", None
        # Only actually executed calls advance the transient response sequence.
        if query == self.target and self.attempts[query, context] < self.timeouts:
            return "TIMEOUT", None
        return "SUCCESS", temperature

    def execute(self, query: Query, context: Context):
        result = self.inspect(query, context)
        self.attempts[query, context] += 1
        return result


def legacy_args(query: Query) -> dict:
    return {"tool": "query_temperature", "args": asdict(query)}


def decide(name: str, query: Query, context: Context, history: list[Record], initial: View, current: View):
    if name == "direct":
        return None
    if name == "hard_exact":
        return next((Rejection(record.record_id, record.code) for record in history
                     if record.query == query and record.code in HARD_FAILURES), None)
    if name == "published_static":
        # Adapter preserves experiment.inspect_action's original shared registry
        # assumption and its original data-snapshot comparison.
        legacy = [{"record_id": record.record_id, "action": legacy_args(record.query),
                   "result": {"code": record.code, "station_id": record.query.station_id,
                              "snapshot": record.context.data_snapshot}} for record in history]
        result = experiment.inspect_action(legacy_args(query), legacy, {"snapshot": context.data_snapshot})
        if result["decision"] == "reject":
            record_id = result["evidence_record_ids"][0]
            return Rejection(record_id, next(record.code for record in history if record.record_id == record_id))
        return None
    if name == "global_flush" and initial != current:
        history = history[1:]
    return rejection_for(query, context, history)


def plan_for(fixture: Fixture):
    if fixture.name == "timeout_retry":
        # The prefix already consumed the one timeout on this exact request.
        return [(fixture.target, "target")]
    if fixture.unknown_probe:
        return [(fixture.candidate, "unknown"), (fixture.target, "target")]
    return ([(fixture.candidate, "known_redundant")] * fixture.redundant
            + [(fixture.target, "target")] * (fixture.timeouts + 1))


def run(fixture: Fixture, arm: str) -> dict:
    simulator = Simulator(fixture.target, fixture.timeouts)
    prefix_context = fixture.before.source(fixture.history_query.source)
    prefix_code, _ = simulator.execute(fixture.history_query, prefix_context)
    assert prefix_code == fixture.history_code, fixture.name
    observed_before = fixture.before.disclose(fixture.history_query.source, fixture.history_missing)
    observed_current = fixture.current.disclose(fixture.candidate.source, fixture.current_missing)
    evidence = Record("h01", fixture.history_query, observed_before.source(fixture.history_query.source), prefix_code)
    history = [evidence]
    trace = []
    tools = 0
    termination = "proposal_sequence_exhausted"

    for index, (query, role) in enumerate(plan_for(fixture)[:experiment.MODEL_LIMIT], 1):
        context = fixture.current.source(query.source)
        observable = observed_current.source(query.source)
        attempts_before = simulator.attempts.copy()
        actual_code, _ = simulator.inspect(query, context)
        if role == "known_redundant":
            assert actual_code == fixture.history_code in HARD_FAILURES
        elif role == "target":
            assert actual_code in {"TIMEOUT", "SUCCESS"}

        rejected = decide(arm, query, observable, history, observed_before, observed_current)
        event = {"proposal": index, "query": asdict(query), "role": role, "context": asdict(observable)}
        if rejected is not None:
            assert any(row.record_id == rejected.record_id for row in history)
            assert simulator.attempts == attempts_before
            event.update(decision="reject", evidence=asdict(rejected), if_executed=actual_code)
        else:
            code, temperature = simulator.execute(query, context)
            assert code == actual_code
            tools += 1
            history.append(Record(f"h{len(history) + 1:02d}", query, observable, code))
            event.update(decision="execute", tool_return=code, temperature_c=temperature)
        event["tools_used"] = tools
        trace.append(event)

        if event.get("tool_return") == "SUCCESS":
            # A stale check that passes on the desired candidate may succeed
            # before consuming the fallback in the dual-world controls.
            assert role in {"target", "unknown"}
            termination = "temperature_found"
            break
        if tools == experiment.TOOL_LIMIT:
            termination = "tool_budget_exhausted"
            break
        if index == experiment.MODEL_LIMIT:
            termination = "proposal_budget_exhausted"
            break

    if arm == "version_scoped":
        for event in trace:
            assert (event["decision"] == "reject") == (event["role"] == "known_redundant"), (fixture.name, event)
    return {
        "success": termination == "temperature_found",
        "termination": termination,
        "tools": tools,
        "proposals": len(trace),
        "wasted_queries": sum(event["decision"] == "execute" and event["role"] == "known_redundant" for event in trace),
        "false_blocks": sum(event.get("if_executed") in {"SUCCESS", "TIMEOUT"} for event in trace),
        "immediate_success_blocks": sum(event.get("if_executed") == "SUCCESS" for event in trace),
        "trace": trace,
    }


def benchmark() -> dict:
    rows = []
    for fixture in fixtures():
        row = asdict(fixture)
        row["id"] = f"{fixture.name}-{fixture.redundant}-{fixture.timeouts}"
        row["proposal_plan"] = [{"query": asdict(query), "role": role} for query, role in plan_for(fixture)]
        row["strategies"] = {arm: run(fixture, arm) for arm in ARMS}
        assert row["strategies"]["version_scoped"]["success"], row["id"]
        rows.append(row)

    metrics = ("success", "tools", "proposals", "wasted_queries", "false_blocks", "immediate_success_blocks")
    summary = {}
    for stratum in ("stale", "retained", "control"):
        group = [row for row in rows if row["stratum"] == stratum]
        summary[stratum] = {
            "cases": len(group),
            "strategies": {arm: {metric: sum(row["strategies"][arm][metric] for row in group)
                                  for metric in metrics} for arm in ARMS},
        }

    return {
        "kind": "paired_versioned_evidence_simulation",
        "budgets": {"tools": experiment.TOOL_LIMIT, "proposals": experiment.MODEL_LIMIT},
        "network_requests": 0,
        "metrics": {
            "wasted_queries": "executed queries for which the fixture supplied applicable hard failure evidence",
            "false_blocks": "rejected queries whose nonmutating simulator probe was SUCCESS or TIMEOUT",
            "immediate_success_blocks": "rejected queries whose nonmutating simulator probe was SUCCESS",
        },
        "world": {
            "registries": [{"namespace": namespace, "revision": revision, "station_ids": ids}
                           for (namespace, revision), ids in REGISTRIES.items()],
            "snapshots": [{"source": source, "snapshot": version,
                           "rows": [{"station": station, "date": day, "variable": variable, "temperature_c": value}
                                    for (station, day, variable), value in values.items()]}
                          for (source, version), values in SNAPSHOTS.items()],
        },
        "summary_by_stratum": summary,
        "cases": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    args.output.write_text(json.dumps(benchmark(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
