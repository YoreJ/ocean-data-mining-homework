"""Offline adapter checks. No network or real model calls."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from experiment import INITIAL_PROMPT, ModelResponse, cases, query, run
from model_connection import (
    JsonAPIAdapter, build_payload, execution_plan, normalize_usage, prepare, verify_frozen,
)


class FakeResponse:
    status = 200
    headers = {"x-request-id": "test-request-id"}

    def __init__(self, body):
        self.body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.body


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.credentials = patch.dict(os.environ, {"PILOT_TEST_KEY": "local-fake-test-secret"})
        self.credentials.start()
        self.addCleanup(self.credentials.stop)
        self.config = {"provider": "test-provider", "base_url": "https://example.invalid/v1",
                       "protocol": "chat_completions", "model": "test-model",
                       "api_key_env": "PILOT_TEST_KEY", "timeout_seconds": 5,
                       "generation": {"temperature": 0, "max_completion_tokens": 512}}

    def observation(self):
        result = run(cases()[1], "M", TextAdapter())
        return result["events"][0]["observation"]

    def adapter(self, body=None, exception=None):
        adapter = JsonAPIAdapter(self.config, self.directory / "requests.jsonl")
        transport = patch.object(adapter.opener, "open", return_value=FakeResponse(body or "{}"), side_effect=exception)
        mocked = transport.start()
        self.addCleanup(transport.stop)
        return adapter, mocked

    def test_frozen_behavior_and_snapshots_unchanged(self):
        result = verify_frozen()
        self.assertTrue(result["snapshots_intact"])
        self.assertTrue(result["behavior_components_unchanged"])

    def test_same_prompt_and_initial_wire_payload(self):
        class Capture:
            def __init__(self):
                self.calls = []

            def generate(self, prompt, observation):
                self.calls.append((prompt, build_payload(self_config, prompt, observation)))
                return ModelResponse(json.dumps(query("B")))

        self_config = self.config
        capture = Capture()
        run(cases()[1], "M", capture)
        run(cases()[1], "MC", capture)
        self.assertEqual(capture.calls[0], capture.calls[1])
        self.assertNotIn("case_id", json.dumps(capture.calls))
        self.assertNotIn("schedules", json.dumps(capture.calls))
        self.assertNotIn("checkpoint", json.dumps(capture.calls))

    def test_payload_rejects_audit_fields(self):
        observation = self.observation()
        observation["group"] = "MC"
        with self.assertRaises(ValueError):
            build_payload(self.config, INITIAL_PROMPT, observation)

    def test_one_call_raw_text_and_usage_saved(self):
        body = {"id": "resp-1", "model": "test-model-revision", "choices": [
            {"message": {"content": json.dumps(query("B"))}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        adapter, transport = self.adapter(json.dumps(body))
        result = run(cases()[1], "M", adapter)
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["totals"]["tokens"]["input_tokens"], 100)
        metadata = result["events"][0]["model_metadata"]
        self.assertEqual(metadata["returned_model"], "test-model-revision")
        self.assertEqual(json.loads(metadata["raw_http_response"]), body)
        self.assertNotIn("local-fake-test-secret", (self.directory / "requests.jsonl").read_text())

    def test_timeout_is_invalid_unknown_and_never_retried(self):
        adapter, transport = self.adapter(exception=TimeoutError())
        result = run(cases()[1], "MC", adapter)
        self.assertEqual(transport.call_count, 1)
        self.assertFalse(result["valid_for_task_statistics"])
        self.assertEqual(result["termination"], "interface_error")
        self.assertIsNone(result["totals"]["tokens"])
        self.assertEqual(result["events"][0]["model_metadata"]["usage_status"], "unknown")
        ledger = [json.loads(line) for line in (self.directory / "requests.jsonl").read_text().splitlines()]
        self.assertEqual([item["event"] for item in ledger], ["attempt_started", "interface_error"])

    def test_http_failure_is_invalid_and_redacted(self):
        failure = HTTPError("https://example.invalid/v1", 429, "error", {},
                            io.BytesIO(b'{"error":"local-fake-test-secret"}'))
        adapter, transport = self.adapter(exception=failure)
        result = run(cases()[1], "M", adapter)
        self.assertFalse(result["valid_for_task_statistics"])
        self.assertEqual(transport.call_count, 1)
        self.assertNotIn("local-fake-test-secret", json.dumps(result))

    def test_bad_provider_envelope_is_interface_error(self):
        adapter, transport = self.adapter('{"usage":{"prompt_tokens":5,"completion_tokens":2}}')
        result = run(cases()[1], "M", adapter)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["termination"], "interface_error")
        self.assertEqual(result["totals"]["tokens"]["input_tokens"], 5)

    def test_bad_assistant_text_is_model_format_error(self):
        body = {"choices": [{"message": {"content": "not JSON"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        adapter, transport = self.adapter(json.dumps(body))
        result = run(cases()[1], "M", adapter)
        self.assertEqual(transport.call_count, 6)
        self.assertEqual(result["termination"], "model_budget_exhausted")
        self.assertTrue(result["valid_for_task_statistics"])
        self.assertTrue(all(event["feedback"]["type"] == "format_error" for event in result["events"]))

    def test_adapter_value_error_is_not_a_model_format_error(self):
        class BrokenAdapter:
            def generate(self, prompt, observation):
                raise ValueError("do not log arbitrary adapter exception text")

        result = run(cases()[1], "M", BrokenAdapter())
        self.assertEqual(result["termination"], "interface_error")
        self.assertEqual(result["events"][0]["model_metadata"]["error_class"], "ValueError")
        self.assertNotIn("do not log", json.dumps(result))

    def test_responses_protocol(self):
        self.config.update(protocol="responses", generation={"max_output_tokens": 512})
        body = {"id": "r-1", "model": "test-model", "status": "completed",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(query("B"))}]}],
                "usage": {"input_tokens": 12, "output_tokens": 7}}
        adapter, transport = self.adapter(json.dumps(body))
        result = run(cases()[1], "MC", adapter)
        self.assertEqual(result["outcome"], "success")
        payload = json.loads(transport.call_args.args[0].data)
        self.assertNotIn("messages", payload)
        self.assertEqual(payload["instructions"], INITIAL_PROMPT)

    def test_anthropic_messages_and_cache_usage(self):
        self.config.update(protocol="anthropic_messages", generation={"max_tokens": 512},
                           anthropic_version="2023-06-01")
        body = {"id": "msg-1", "type": "message", "model": "test-model", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(query("B"))}],
                "usage": {"input_tokens": 12, "output_tokens": 7,
                          "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5}}
        adapter, transport = self.adapter(json.dumps(body))
        result = run(cases()[1], "MC", adapter)
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["totals"]["tokens"]["input_tokens"], 20)
        payload = json.loads(transport.call_args.args[0].data)
        self.assertEqual(payload["system"], INITIAL_PROMPT)
        self.assertNotIn("n", payload)

    def test_local_credential_is_referenced_without_copying(self):
        from model_connection import credential

        fake_settings = json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "local-test-token"}})
        with patch.object(Path, "read_text", return_value=fake_settings):
            config = {"credential_source": "claude_settings"}
            self.assertEqual(credential(config), "local-test-token")
            self.assertNotIn("local-test-token", json.dumps(config))

    def test_unknown_usage_is_not_zero(self):
        self.assertIsNone(normalize_usage(None, "responses"))
        self.assertIsNone(normalize_usage({"prompt_tokens": 2}, "chat_completions"))

    def test_plan_has_sixteen_balanced_alternating_runs(self):
        plan = execution_plan()
        self.assertEqual(len(plan), 16)
        self.assertEqual(len(plan) * 6, 96)
        self.assertEqual([row["group"] for row in plan[:4]], ["M", "MC", "MC", "M"])
        for index in range(0, 16, 2):
            self.assertEqual(plan[index]["checkpoint_hash"], plan[index + 1]["checkpoint_hash"])
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in plan))

    def test_preflight_never_calls_network(self):
        path = self.directory / "config.json"
        path.write_text(json.dumps(self.config))
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            report = prepare(path, self.directory / "preflight")
        self.assertEqual(report["model_requests_sent"], 0)
        self.assertEqual(report["status"], "BLOCKED")


class TextAdapter:
    def generate(self, prompt, observation):
        return ModelResponse(json.dumps(query("B")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
