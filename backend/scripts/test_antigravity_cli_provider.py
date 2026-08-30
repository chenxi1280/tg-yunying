from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_AGY_BIN = Path.home() / ".local" / "bin" / "agy"
DEFAULT_TIMEOUT_SECONDS = 30
PROCESS_TERMINATE_GRACE_SECONDS = 5
OUTER_TIMEOUT_GRACE_SECONDS = 15
POC_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "reason": {"type": "string"},
    },
    "required": ["status", "reason"],
    "additionalProperties": False,
}
POC_PROMPT = (
    "Do not use tools. Return status='ok' and a short Chinese reason that confirms "
    "this Antigravity CLI structured-output probe completed."
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


class PocError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PocConfig:
    agy_bin: Path
    profile_home: Path | None
    model: str
    effort: str
    timeout_seconds: int


def parse_args() -> PocConfig:
    parser = argparse.ArgumentParser(
        description="Run a bounded, no-tools Antigravity CLI structured-output POC.",
    )
    parser.add_argument("--agy-bin", type=Path, default=DEFAULT_AGY_BIN)
    parser.add_argument("--profile-home", type=Path)
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.profile_home is not None and not args.profile_home.is_dir():
        parser.error("--profile-home must be an existing directory")
    return PocConfig(
        agy_bin=args.agy_bin.expanduser(),
        profile_home=args.profile_home.expanduser() if args.profile_home else None,
        model=str(args.model).strip(),
        effort=str(args.effort),
        timeout_seconds=int(args.timeout_seconds),
    )


def build_command(config: PocConfig) -> list[str]:
    command = [
        str(config.agy_bin),
        "--sandbox",
        "--disable-slash-commands",
        "--print-timeout",
        f"{config.timeout_seconds}s",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(POC_SCHEMA, ensure_ascii=False, separators=(",", ":")),
        "--effort",
        config.effort,
    ]
    if config.model:
        command.extend(("--model", config.model))
    command.extend(("-p", POC_PROMPT))
    return command


def process_environment(config: PocConfig) -> dict[str, str]:
    environment = dict(os.environ)
    if config.profile_home is not None:
        environment["HOME"] = str(config.profile_home)
    return environment


def run_command(config: PocConfig, workdir: str) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            build_command(config),
            cwd=workdir,
            env=process_environment(config),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise PocError("antigravity_binary_missing") from exc
    try:
        stdout, stderr = process.communicate(
            timeout=config.timeout_seconds + OUTER_TIMEOUT_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        raise PocError("antigravity_poc_timeout") from exc
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()


def parse_envelope(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    envelope = load_json_object(completed.stdout)
    if completed.returncode != 0:
        raise PocError(classify_cli_failure(envelope, completed.stderr))
    if envelope.get("status") != "SUCCESS":
        raise PocError(classify_cli_failure(envelope, completed.stderr))
    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        raise PocError("antigravity_structured_output_missing")
    validate_structured_output(structured)
    return envelope


def load_json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PocError("antigravity_invalid_envelope") from exc
    if not isinstance(payload, dict):
        raise PocError("antigravity_invalid_envelope")
    return payload


def classify_cli_failure(envelope: dict[str, Any], stderr: str) -> str:
    detail = " ".join((str(envelope.get("error") or ""), stderr)).lower()
    if "not eligible" in detail or "eligibility check failed" in detail:
        return "antigravity_account_ineligible"
    if "authentication" in detail or "log in" in detail:
        return "antigravity_auth_required"
    if "quota" in detail or "rate limit" in detail or "credits" in detail:
        return "antigravity_quota_limited"
    if "model" in detail and ("invalid" in detail or "not recognized" in detail):
        return "antigravity_model_invalid"
    return "antigravity_cli_exit_nonzero"


def validate_structured_output(payload: dict[str, Any]) -> None:
    if payload.get("status") != "ok":
        raise PocError("antigravity_schema_invalid")
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        raise PocError("antigravity_schema_invalid")


def public_result(envelope: dict[str, Any], config: PocConfig) -> dict[str, Any]:
    usage = dict(envelope.get("usage") or {})
    return {
        "status": "SUCCESS",
        "model": config.model or "cli_default",
        "duration_seconds": envelope.get("duration_seconds"),
        "num_turns": envelope.get("num_turns"),
        "usage": {field: int(usage.get(field) or 0) for field in USAGE_FIELDS},
        "structured_output": envelope["structured_output"],
    }


def main() -> int:
    config = parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="tgyunying-antigravity-poc-") as workdir:
            completed = run_command(config, workdir)
        envelope = parse_envelope(completed)
    except PocError as exc:
        print(json.dumps({"status": "ERROR", "error_code": exc.code}), file=sys.stderr)
        return 1
    print(json.dumps(public_result(envelope, config), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
