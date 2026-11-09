"""Offline checks for the real-run orchestration, never contacting a model."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiment import INITIAL_PROMPT, ModelInterfaceError, ModelResponse, digest, query
from run_real import batch, connectivity


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.config = {"model": "offline-test-model"}

    def test_connectivity_records_model_format_separately(self):
        with patch("run_real.JsonAPIAdapter") as cls:
            cls.return_value.generate.return_value = ModelResponse("not JSON")
            with patch("builtins.print"):
                result = connectivity(self.config, self.output, "offline-format")
        self.assertEqual(result["status"], "MODEL_FORMAT_ERROR")
        self.assertFalse(result["included_in_experiment"])
        self.assertEqual(result["model_requests"], 1)

    def test_failed_probe_prevents_batch(self):
        with patch("run_real.JsonAPIAdapter") as cls:
            cls.return_value.generate.side_effect = ModelInterfaceError("offline failure")
            with patch("builtins.print"):
                connectivity(self.config, self.output, "offline-error")
            with self.assertRaises(ValueError):
                batch(self.config, self.output, "offline-error")
        self.assertFalse((self.output / "experiment").exists())

    def test_batch_stops_after_first_interface_error_and_preserves_order(self):
        probe_dir = self.output / "connectivity" / "offline-ok"
        probe_dir.mkdir(parents=True)
        (probe_dir / "result.json").write_text(json.dumps({
            "status": "PASSED", "config_hash": digest(self.config), "prompt_hash": digest(INITIAL_PROMPT)}))
        with patch("run_real.JsonAPIAdapter") as cls:
            cls.return_value.generate.side_effect = ModelInterfaceError("offline failure")
            with patch("builtins.print"):
                result = batch(self.config, self.output, "offline-ok")
            self.assertEqual(cls.return_value.generate.call_count, 1)
        manifest = json.loads((self.output / "experiment/manifest.json").read_text())
        self.assertEqual(manifest["status"], "STOPPED_INTERFACE_ERROR")
        self.assertEqual(manifest["plan"][0]["actual_order"], 1)
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in manifest["plan"][1:]))
        self.assertFalse(result["rows"][0]["valid"])


if __name__ == "__main__":
    unittest.main()
