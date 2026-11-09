"""Program checks only. No real model is invoked."""

import copy
import unittest

from experiment import (
    Case, Rules, ScriptedAdapter, append_history, cases, digest, finish,
    initialize, inspect_action, query, resolve, run,
)


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.case = cases()[3]
        self.env, self.history = initialize(self.case)
        append_history(self.history, resolve(), self.env.execute(resolve()))
        append_history(self.history, query(), self.env.execute(query()))

    def check_action(self, action, decision, constraint=None):
        before = digest(self.env.state())
        result = inspect_action(action, self.history, self.case.task)
        self.assertEqual(result["decision"], decision)
        self.assertEqual(result.get("constraint"), constraint)
        self.assertEqual(digest(self.env.state()), before)

    def test_v1_invalid_old_identifier(self):
        self.check_action(query(station_id="OLD7"), "reject", "C1")

    def test_v2_corrected_identifier(self):
        env, history = initialize(cases()[0])
        append_history(history, resolve(), env.execute(resolve()))
        self.assertEqual(inspect_action(query(), history, self.case.task)["decision"], "allow")

    def test_v3_invalid_id_shared_namespace(self):
        self.check_action(query("B", "OLD7"), "reject", "C1")

    def test_v4_known_missing_query(self):
        self.check_action(query(), "reject", "C2")

    def test_v5_source_switch(self):
        self.check_action(query("B"), "allow")

    def test_v6_changed_date(self):
        self.check_action(query(date="2026-01-16"), "allow")

    def test_v7_timeout_retry(self):
        env, history = initialize(cases()[2])
        self.assertEqual(inspect_action(query(), history, self.case.task)["decision"], "allow")

    def test_v8_backup_retry_after_primary_missing(self):
        append_history(self.history, query("B"), self.env.execute(query("B")))
        self.check_action(query("B"), "allow")

    def test_corrected_id_not_matched_to_old_no_record(self):
        # Test a changed actual identifier. Its validity is unknown to checker.
        self.check_action(query(station_id="OTHER-ID"), "allow")


class ExecutionTests(unittest.TestCase):
    def test_eight_rule_cases(self):
        expected_counts = [2, 1, 1, 4, 2, 2, 1, 4]
        for case, count in zip(cases(), expected_counts):
            with self.subTest(case=case.case_id):
                result = run(case, "R")
                self.assertEqual(result["outcome"], case.expected)
                self.assertEqual(result["totals"]["tool_calls"], count)
                self.assertEqual(result["totals"]["model_calls"], 0)

    def test_rejection_preserves_state_and_tools(self):
        result = run(cases()[1], "MC", ScriptedAdapter([query(), query("B")]))
        rejected = result["events"][0]
        self.assertEqual(rejected["check"]["constraint"], "C2")
        self.assertFalse(rejected["executed"])
        self.assertEqual(rejected["audit"]["state_before"], rejected["audit"]["state_after"])
        self.assertEqual(rejected["budgets_after"], {"tools": 4, "models": 5})
        self.assertEqual(result["totals"]["model_calls"], 2)
        self.assertEqual(result["totals"]["tool_calls"], 1)
        self.assertEqual(result["outcome"], "success")

    def test_unrelated_requests_and_sources_have_separate_counters(self):
        case = cases()[3]
        env, _ = initialize(case)
        env.execute(query("P"))
        env.execute(query("B", date="2026-01-16"))
        env.execute(resolve())
        env.execute(query("B", "OLD7"))
        self.assertEqual(env.execute(query("B"))["code"], "TIMEOUT")
        self.assertEqual(env.execute(query("B"))["code"], "SUCCESS")

    def test_prefix_consumes_initial_timeout(self):
        env, history = initialize(cases()[2])
        self.assertEqual(history[-1]["result"]["code"], "TIMEOUT")
        self.assertEqual(env.execute(query())["code"], "SUCCESS")

    def test_same_checkpoints_all_groups_all_cases(self):
        for case in cases():
            results = [run(case, "R"),
                       run(case, "M", ScriptedAdapter([finish()])),
                       run(case, "MC", ScriptedAdapter([finish()]))]
            with self.subTest(case=case.case_id):
                self.assertEqual(len({r["initial_checkpoint_hash"] for r in results}), 1)
                self.assertTrue(all(r["prefix"] == results[0]["prefix"] for r in results))

    def test_fourth_tool_success_is_collected(self):
        actions = [resolve(), query(), query("B"), query("B")]
        for group in ("R", "M", "MC"):
            result = run(cases()[3], group, None if group == "R" else ScriptedAdapter(actions))
            self.assertEqual(result["outcome"], "success")
            self.assertEqual(result["termination"], "evidence_complete")
            self.assertEqual(result["totals"]["tool_calls"], 4)

    def test_fourth_tool_completes_missing_evidence_all_groups(self):
        # Boundary fixture, separate from the eight pilot cases.
        case = Case("boundary", "last call proves absence", [resolve(), query()],
                    {"P": ("TIMEOUT", "TIMEOUT", "NO_RECORD"),
                     "B": ("TIMEOUT", "NO_RECORD")}, "correct_no_record")
        actions = [query(), query("B"), query("B"), query()]
        for group in ("R", "M", "MC"):
            result = run(case, group, None if group == "R" else ScriptedAdapter(actions))
            self.assertEqual(result["outcome"], "correct_no_record")
            self.assertEqual(result["termination"], "evidence_complete")
            self.assertEqual(result["totals"]["tool_calls"], 4)

    def test_rules_retry_backup_after_switch(self):
        case = Case("transition", "source priority boundary", [resolve(), query()],
                    {"P": ("TIMEOUT", "TIMEOUT", "SUCCESS"),
                     "B": ("TIMEOUT", "SUCCESS")}, "success")
        result = run(case, "R")
        sources = [event["action"]["args"]["source"] for event in result["events"]]
        self.assertEqual(sources, ["P", "B", "B"])
        observation = result["events"][2]["observation"]
        self.assertEqual(Rules().choose(observation), Rules().choose(copy.deepcopy(observation)))

    def test_six_rejected_proposals_exhaust_model_budget(self):
        result = run(cases()[1], "MC", ScriptedAdapter([query()] * 6))
        self.assertEqual(result["totals"]["tool_calls"], 0)
        self.assertEqual(result["totals"]["model_calls"], 6)
        self.assertEqual(result["termination"], "model_budget_exhausted")

    def test_format_errors_count_as_model_calls(self):
        result = run(cases()[1], "M", ScriptedAdapter(["bad JSON", query("B")]))
        self.assertEqual(result["events"][0]["feedback"]["type"], "format_error")
        self.assertEqual(result["totals"]["model_calls"], 2)
        self.assertEqual(result["totals"]["tool_calls"], 1)
        self.assertEqual(result["outcome"], "success")

    def test_sixth_model_action_still_executes(self):
        result = run(cases()[1], "M", ScriptedAdapter(["bad"] * 5 + [query("B")]))
        self.assertEqual(result["totals"]["model_calls"], 6)
        self.assertEqual(result["outcome"], "success")

    def test_format_errors_exhaust_budget_without_tools(self):
        result = run(cases()[1], "MC", ScriptedAdapter(["bad"] * 6))
        self.assertEqual(result["totals"]["tool_calls"], 0)
        self.assertEqual(result["totals"]["model_calls"], 6)
        self.assertEqual(result["termination"], "model_budget_exhausted")

    def test_invalid_date_is_common_format_error(self):
        result = run(cases()[1], "M", ScriptedAdapter([query(date="2026-02-30"), query("B")]))
        self.assertEqual(result["events"][0]["feedback"]["type"], "format_error")
        self.assertEqual(result["totals"]["tool_calls"], 1)

    def test_unsupported_claim_on_sixth_decision_is_still_an_error(self):
        result = run(cases()[1], "M", ScriptedAdapter(["bad"] * 5 + [finish("no_record")]))
        self.assertEqual(result["outcome"], "unsupported_no_record")
        self.assertEqual(len(result["claim_errors"]), 1)

    def test_unsupported_missing_claim_preserved(self):
        result = run(cases()[2], "M", ScriptedAdapter([finish("no_record")]))
        self.assertEqual(result["outcome"], "unsupported_no_record")
        self.assertEqual(len(result["claim_errors"]), 1)
        self.assertEqual(result["totals"]["tool_calls"], 0)

    def test_active_incomplete_and_exhaustion_share_outcome(self):
        active = run(cases()[7], "M", ScriptedAdapter([finish()]))
        exhausted = run(cases()[7], "M", ScriptedAdapter([query()] * 4))
        self.assertEqual(active["outcome"], exhausted["outcome"])
        self.assertEqual(active["termination"], "active_finish")
        self.assertEqual(exhausted["termination"], "tool_budget_exhausted")

    def test_model_observation_has_no_hidden_schedule_or_audit(self):
        result = run(cases()[3], "MC", ScriptedAdapter([finish()]))
        observation = result["events"][0]["observation"]
        self.assertNotIn("schedules", str(observation))
        self.assertNotIn("audit", observation)

    def test_scripts_cannot_be_saved_as_real_model_experiments(self):
        with self.assertRaises(ValueError):
            run(cases()[0], "M", ScriptedAdapter([]), run_kind="real_model")

    def test_missing_real_adapter_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "not been configured"):
            run(cases()[0], "M")


if __name__ == "__main__":
    unittest.main(verbosity=2)
