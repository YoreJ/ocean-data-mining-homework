"""Local preflight and no-retry API adapters. The CLI never sends requests."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import ssl
import time
import uuid
from pathlib import Path
from urllib import error, parse, request

from experiment import (
    INITIAL_PROMPT, MODEL_LIMIT, ModelInterfaceError, ModelResponse,
    cases, digest, initialize,
)

ROOT = Path(__file__).resolve().parent


def credential(config: dict) -> str | None:
    if config.get("credential_source", "environment") == "claude_settings":
        try:
            settings = json.loads((Path.home() / ".claude/settings.json").read_text())
            env = settings.get("env", {})
            return env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        except (OSError, ValueError):
            return None
    return os.environ.get(config.get("api_key_env", ""))


def verify_frozen() -> dict:
    manifest = json.loads((ROOT / "frozen/v0.1/manifest.json").read_text())
    components = {}
    for node in ast.parse((ROOT / "experiment.py").read_text()).body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in manifest["behavior_components"]:
            components[name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    snapshots_ok = all(
        hashlib.sha256((ROOT / "frozen/v0.1" / name).read_bytes()).hexdigest() == expected
        for name, expected in manifest["files"].items()
    )
    return {"version": "v0.1", "snapshots_intact": snapshots_ok,
            "behavior_components_unchanged": components == manifest["behavior_components"]}


def validate_config(config: dict) -> list[str]:
    issues = []
    allowed = {"provider", "base_url", "protocol", "model", "api_key_env",
               "timeout_seconds", "generation", "pricing", "credential_source",
               "anthropic_version", "auth_header", "ca_file"}
    if set(config) - allowed:
        issues.append("Unknown config fields; keep secrets in environment variables only")
    for key in ("provider", "base_url", "model", "api_key_env"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            issues.append(f"Missing {key}")
    url = parse.urlsplit(config.get("base_url", ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        issues.append("base_url must be HTTPS without credentials, query, or fragment")
    protocol = config.get("protocol")
    if protocol not in ("chat_completions", "responses", "anthropic_messages"):
        issues.append("Select chat_completions, responses, or anthropic_messages protocol")
    generation = config.get("generation", {})
    if not isinstance(generation, dict):
        return issues + ["generation must be an object"]
    generation_fields = {
        "chat_completions": {"temperature", "top_p", "seed", "max_completion_tokens", "max_tokens", "reasoning_effort"},
        "responses": {"temperature", "top_p", "max_output_tokens", "reasoning"},
        "anthropic_messages": {"temperature", "top_p", "max_tokens"},
    }
    allowed_generation = generation_fields.get(protocol, set())
    if set(generation) - allowed_generation:
        issues.append("Unsupported generation fields; do not override messages, model, n, tools, or streaming")
    cap_fields = [key for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if key in generation]
    if (len(cap_fields) != 1 or type(generation[cap_fields[0]]) is not int
            or generation[cap_fields[0]] <= 0):
        issues.append("Choose exactly one positive output-token cap supported by the selected protocol/model")
    if not isinstance(config.get("timeout_seconds"), (int, float)) or config["timeout_seconds"] <= 0:
        issues.append("Choose a positive timeout_seconds")
    if config.get("credential_source", "environment") not in ("environment", "claude_settings"):
        issues.append("Unknown credential source")
    if not credential(config):
        issues.append("API credential is not present in the selected local source")
    if config.get("auth_header", "bearer") not in ("bearer", "x-api-key"):
        issues.append("Unsupported authentication header")
    if protocol == "anthropic_messages" and not config.get("anthropic_version"):
        issues.append("Choose anthropic_version supported by the gateway")
    if config.get("ca_file") and not Path(config["ca_file"]).is_file():
        issues.append("Configured CA certificate file does not exist")
    return issues


def build_payload(config: dict, prompt: str, observation: dict) -> dict:
    # Whitelist the whole public observation. Audit data is never transmitted.
    public_fields = {"task", "contract", "history", "budgets", "decisions"}
    if set(observation) != public_fields:
        raise ValueError("Observation contains unexpected fields")
    user_text = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    payload = {"model": config["model"], "stream": False, **config["generation"]}
    if config["protocol"] == "chat_completions":
        payload.update(n=1, messages=[{"role": "system", "content": prompt},
                                     {"role": "user", "content": user_text}])
    elif config["protocol"] == "responses":
        payload.update(instructions=prompt, input=[{"role": "user", "content": user_text}])
    else:
        payload.update(system=prompt, messages=[{"role": "user", "content": user_text}])
    return payload


def normalize_usage(raw: dict | None, protocol: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    input_key, output_key = (("prompt_tokens", "completion_tokens")
                             if protocol == "chat_completions" else ("input_tokens", "output_tokens"))
    if any(type(raw.get(key)) is not int or raw[key] < 0 for key in (input_key, output_key)):
        return None
    # Reasoning/cache details are retained in raw_usage, not added twice.
    normalized = {"input_tokens": raw[input_key], "output_tokens": raw[output_key], "other_tokens": 0}
    if protocol == "anthropic_messages":
        cache_tokens = [raw.get(key, 0) for key in ("cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(type(value) is not int or value < 0 for value in cache_tokens):
            return None
        normalized["input_tokens"] += sum(cache_tokens)
    return normalized


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonAPIAdapter:
    """One generate call => at most one HTTP attempt, with durable local journal.

    This class is available for later authorized use. Preparation invokes neither
    this class nor the provider's model-list or inference endpoints.
    """

    def __init__(self, config: dict, journal: Path):
        issues = validate_config(config)
        if issues:
            raise ValueError("; ".join(issues))
        self.config = json.loads(json.dumps(config))
        self.api_key = credential(config)
        self.journal = journal
        journal.parent.mkdir(parents=True, exist_ok=True)
        context = ssl.create_default_context(cafile=config.get("ca_file"))
        self.opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=context))

    def record(self, event: dict) -> None:
        # Credentials are never part of the payload. Redact accidental echoes.
        line = json.dumps(event, ensure_ascii=False).replace(self.api_key, "[REDACTED]")
        descriptor = os.open(self.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def generate(self, initial_prompt: str, observation: dict) -> ModelResponse:
        config = self.config
        payload = build_payload(config, initial_prompt, observation)
        suffix = {"chat_completions": "/chat/completions", "responses": "/responses",
                  "anthropic_messages": "/messages"}[config["protocol"]]
        endpoint = config["base_url"].rstrip("/") + suffix
        client_id = str(uuid.uuid4())
        metadata = {"client_request_id": client_id, "provider": config["provider"],
                    "requested_model": config["model"], "protocol": config["protocol"],
                    "generation": config["generation"], "timeout_seconds": config["timeout_seconds"],
                    "automatic_retries": 0, "submission_status": "attempted_outcome_unknown",
                    "usage_status": "unknown"}
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "X-Client-Request-Id": client_id,
                   "User-Agent": "RecoveryPilot/0.1"}
        if config.get("auth_header", "bearer") == "bearer":
            headers["Authorization"] = "Bearer " + self.api_key
        else:
            headers["x-api-key"] = self.api_key
        if config["protocol"] == "anthropic_messages":
            headers["anthropic-version"] = config["anthropic_version"]
        req = request.Request(endpoint, data=encoded, method="POST", headers=headers)
        self.record({"event": "attempt_started", "at_unix": time.time(),
                     "metadata": metadata, "payload": payload})
        try:
            with self.opener.open(req, timeout=config["timeout_seconds"]) as response:
                raw = response.read().decode("utf-8", errors="replace")
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                status = response.status
        except error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace").replace(self.api_key, "[REDACTED]")
            finally:
                exc.close()
            metadata.update(http_status=exc.code, provider_request_id=exc.headers.get("x-request-id"),
                            submission_status="http_error_received", raw_http_response=raw)
            self.record({"event": "interface_error", "at_unix": time.time(), "metadata": metadata})
            raise ModelInterfaceError("Provider HTTP error; no retry; run invalid", metadata) from None
        except (OSError, error.URLError, TimeoutError) as exc:
            metadata["error_class"] = type(exc).__name__
            metadata["transport_error_message"] = str(exc).replace(self.api_key, "[REDACTED]")
            metadata["transport_reason_class"] = type(getattr(exc, "reason", exc)).__name__
            self.record({"event": "interface_error", "at_unix": time.time(), "metadata": metadata})
            raise ModelInterfaceError("Transport failed; submission/usage may be unknown; no retry; run invalid", metadata) from None
        raw = raw.replace(self.api_key, "[REDACTED]")
        metadata.update(http_status=status, provider_request_id=request_id,
                        submission_status="response_received", raw_http_response=raw)
        # Persist the raw response before parsing its envelope or action.
        self.record({"event": "response_received", "at_unix": time.time(), "metadata": metadata})
        usage = None
        try:
            body = json.loads(raw)
            raw_usage = body.get("usage")
            usage = normalize_usage(raw_usage, config["protocol"])
            metadata.update(returned_model=body.get("model"), response_id=body.get("id"),
                            raw_usage=raw_usage, usage_status="known" if usage is not None else "unknown")
            if config["protocol"] == "chat_completions":
                if len(body["choices"]) != 1:
                    raise ValueError("Expected one choice")
                choice = body["choices"][0]
                text = choice["message"].get("content")
                metadata["finish_reason"] = choice.get("finish_reason")
                metadata["refusal"] = choice["message"].get("refusal")
                if text is None and metadata["refusal"]:
                    text = ""
            elif config["protocol"] == "responses":
                metadata["finish_reason"] = body.get("status")
                text = "".join(part["text"] for item in body["output"] if item.get("type") == "message"
                               for part in item.get("content", []) if part.get("type") == "output_text")
            else:
                if body.get("type") != "message":
                    raise ValueError("Expected message envelope")
                metadata["finish_reason"] = body.get("stop_reason")
                text = "".join(part["text"] for part in body["content"] if part.get("type") == "text")
            if not isinstance(text, str):
                raise ValueError("Missing assistant text")
        except (ValueError, TypeError, KeyError, AttributeError):
            self.record({"event": "interface_error", "at_unix": time.time(), "metadata": metadata})
            raise ModelInterfaceError("Invalid provider response envelope; no retry; run invalid", metadata, usage) from None
        self.record({"event": "response_parsed", "at_unix": time.time(), "metadata": metadata})
        return ModelResponse(text, usage, metadata)


def execution_plan() -> list[dict]:
    plan = []
    for index, case in enumerate(cases()):
        groups = ("M", "MC") if index % 2 == 0 else ("MC", "M")
        env, prefix = initialize(case)
        for group in groups:
            plan.append({"planned_order": len(plan) + 1, "case_id": case.case_id,
                         "group": group, "run_id": f"{case.case_id}-{group}",
                         "checkpoint_hash": digest(env.state()), "prefix_hash": digest(prefix),
                         "actual_started_at": None, "status": "NOT_RUN"})
    return plan


def prepare(config_path: Path, output: Path) -> dict:
    config = json.loads(config_path.read_text())
    issues = validate_config(config)
    frozen = verify_frozen()
    if not all((frozen["snapshots_intact"], frozen["behavior_components_unchanged"])):
        issues.append("Frozen experiment integrity check failed")
    pricing = config.get("pricing", {})
    if (pricing.get("amount_status") != "unknown_authorized_by_user"
            and not all(pricing.get(key) is not None for key in (
                "currency", "input_per_million", "output_per_million", "approved_total"))):
        issues.append("Pricing and total cost condition have not been agreed")
    report = {"status": "BLOCKED" if issues else "LOCAL_CHECKS_PASSED_REMOTE_UNVERIFIED",
              "issues": issues, "remote_validation": "NOT_PERFORMED",
              "model_requests_sent": 0, "max_model_requests": 16 * MODEL_LIMIT,
              "usage_capability": "API usage parser prepared; actual provider reporting unverified",
              "frozen": frozen, "prompt_hash": digest(INITIAL_PROMPT),
              "provider": config.get("provider"), "model": config.get("model"),
              "protocol": config.get("protocol"), "generation": config.get("generation"),
              "timeout_seconds": config.get("timeout_seconds"), "pricing": pricing,
              "automatic_retries": 0, "plan": execution_plan(),
              "execution_policy": "Stop batch on interface error; keep request ledger; no automatic rerun"}
    output.mkdir(parents=True, exist_ok=False)
    (output / "preflight.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (output / "model_prompt.txt").write_text(INITIAL_PROMPT)
    print(json.dumps({"status": report["status"], "issues": issues,
                      "model_requests_sent": 0, "maximum_model_requests": 96}, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "model_config.example.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / time.strftime("preflight-%Y%m%d-%H%M%S"))
    arguments = parser.parse_args()
    prepare(arguments.config, arguments.output)
