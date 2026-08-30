from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from scripts.antigravity_provider_ledger import LedgerRecord, RequestLedger
except ModuleNotFoundError:  # Direct host execution from backend/scripts.
    from antigravity_provider_ledger import LedgerRecord, RequestLedger


PRIMARY_MODEL = "gemini-3.5-flash-medium"
SECONDARY_MODEL = "gemini-3.1-pro-low"
ALLOWED_MODELS = frozenset({PRIMARY_MODEL, SECONDARY_MODEL})
MAX_BODY_BYTES = 1_000_000
PROCESS_GRACE_SECONDS = 20
PROCESS_TERMINATE_SECONDS = 5
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


class BridgeError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class BridgeConfig:
    slot_id: str
    token: str
    agy_bin: Path
    ledger_path: Path
    ledger_key: str
    max_timeout_seconds: int


class AntigravityRuntime:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.ledger = RequestLedger(config.ledger_path, config.ledger_key)
        self.capacity = threading.Lock()

    def generate(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request_id, request_hash = self._validate(payload)
        existing = self.ledger.get(request_id)
        if existing is not None:
            return self._existing(existing, request_hash)
        if not self.capacity.acquire(blocking=False):
            raise BridgeError("antigravity_capacity_busy", HTTPStatus.TOO_MANY_REQUESTS)
        try:
            record = self.ledger.start(request_id, request_hash)
            if record.state != "started" or record.response is not None:
                return self._existing(record, request_hash)
            return self._execute(request_id, payload)
        finally:
            self.capacity.release()

    def request_status(self, request_id: str) -> tuple[int, dict[str, Any]]:
        record = self.ledger.get(request_id)
        if record is None:
            return HTTPStatus.NOT_FOUND, {"request_id": request_id, "state": "not_started"}
        return self._record_response(record)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "slot_id": self.config.slot_id,
            "binary_ready": self.config.agy_bin.is_file(),
            "inflight": self.capacity.locked(),
            "models": [PRIMARY_MODEL, SECONDARY_MODEL],
        }

    def _validate(self, payload: dict[str, Any]) -> tuple[str, str]:
        request_id = str(payload.get("request_id") or "").strip()
        model = str(payload.get("model") or "").strip()
        schema = payload.get("json_schema")
        if not request_id or len(request_id) > 200:
            raise BridgeError("antigravity_request_id_invalid", HTTPStatus.BAD_REQUEST)
        if model not in ALLOWED_MODELS:
            raise BridgeError("antigravity_model_invalid", HTTPStatus.UNPROCESSABLE_ENTITY)
        if not isinstance(schema, dict):
            raise BridgeError("antigravity_schema_missing", HTTPStatus.BAD_REQUEST)
        timeout = int(payload.get("timeout_seconds") or 0)
        if timeout <= 0 or timeout > self.config.max_timeout_seconds:
            raise BridgeError("antigravity_timeout_invalid", HTTPStatus.BAD_REQUEST)
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return request_id, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _execute(self, request_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        started = time.monotonic()
        try:
            completed = self._run_cli(payload)
            response = self._parse_cli(request_id, payload, completed, started)
        except subprocess.TimeoutExpired:
            self.ledger.settle(
                request_id,
                state="unknown",
                error_code="antigravity_provider_result_unknown",
            )
            return HTTPStatus.ACCEPTED, {
                "request_id": request_id,
                "state": "unknown",
                "error_code": "antigravity_provider_result_unknown",
            }
        except BridgeError as exc:
            self.ledger.settle(request_id, state="failed", error_code=exc.code)
            raise
        self.ledger.settle(request_id, state="confirmed", response=response)
        return HTTPStatus.OK, response

    def _run_cli(self, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        timeout = int(payload["timeout_seconds"])
        command = [
            str(self.config.agy_bin),
            "--sandbox",
            "--disable-slash-commands",
            "--print-timeout",
            f"{timeout}s",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(payload["json_schema"], ensure_ascii=False, separators=(",", ":")),
            "--model",
            str(payload["model"]),
        ]
        if payload["model"] == PRIMARY_MODEL:
            command.extend(("--effort", "medium"))
        prompt = self._prompt(payload)
        command.extend(("-p", prompt))
        with tempfile.TemporaryDirectory(prefix=f"agy-{self.config.slot_id}-") as workdir:
            process = subprocess.Popen(
                command,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout + PROCESS_GRACE_SECONDS)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                raise exc
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _prompt(self, payload: dict[str, Any]) -> str:
        system = str(payload.get("system_prompt") or "").strip()
        user = str(payload.get("user_prompt") or "").strip()
        return f"System instructions:\n{system}\n\nUser request:\n{user}".strip()

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=PROCESS_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()

    def _parse_cli(
        self,
        request_id: str,
        payload: dict[str, Any],
        completed: subprocess.CompletedProcess[str],
        started: float,
    ) -> dict[str, Any]:
        envelope = self._load_object(completed.stdout)
        if completed.returncode != 0 or envelope.get("status") != "SUCCESS":
            raise BridgeError(
                self._classify_failure(envelope, completed.stderr),
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            raise BridgeError("antigravity_schema_missing", HTTPStatus.UNPROCESSABLE_ENTITY)
        usage = dict(envelope.get("usage") or {})
        return {
            "request_id": request_id,
            "state": "confirmed",
            "slot_id": self.config.slot_id,
            "model": str(payload["model"]),
            "structured_output": structured,
            "usage": {field: int(usage.get(field) or 0) for field in USAGE_FIELDS},
            "duration_seconds": round(time.monotonic() - started, 3),
            "num_turns": int(envelope.get("num_turns") or 0),
        }

    def _load_object(self, raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError("antigravity_invalid_envelope", HTTPStatus.UNPROCESSABLE_ENTITY) from exc
        if not isinstance(payload, dict):
            raise BridgeError("antigravity_invalid_envelope", HTTPStatus.UNPROCESSABLE_ENTITY)
        return payload

    def _classify_failure(self, envelope: dict[str, Any], stderr: str) -> str:
        detail = " ".join((str(envelope.get("error") or ""), stderr)).lower()
        if "not eligible" in detail or "eligibility check failed" in detail:
            return "antigravity_account_ineligible"
        if "authentication" in detail or "sign in" in detail or "log in" in detail:
            return "antigravity_auth_required"
        if "quota" in detail or "rate limit" in detail or "credits" in detail:
            return "antigravity_quota_limited"
        if "model" in detail and ("invalid" in detail or "not recognized" in detail):
            return "antigravity_model_invalid"
        return "antigravity_cli_exit_nonzero"

    def _existing(self, record: LedgerRecord, request_hash: str) -> tuple[int, dict[str, Any]]:
        if record.request_hash != request_hash:
            raise BridgeError("antigravity_request_id_reused", HTTPStatus.CONFLICT)
        return self._record_response(record)

    def _record_response(self, record: LedgerRecord) -> tuple[int, dict[str, Any]]:
        if record.state == "confirmed" and record.response is not None:
            return HTTPStatus.OK, record.response
        status = HTTPStatus.ACCEPTED if record.state in {"started", "unknown"} else HTTPStatus.UNPROCESSABLE_ENTITY
        return status, {
            "request_id": record.request_id,
            "state": record.state,
            "error_code": record.error_code,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "TgYunyingAntigravity/1"

    @property
    def runtime(self) -> AntigravityRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/internal/v1/health":
            self._send(HTTPStatus.OK, self.runtime.health())
            return
        prefix = "/internal/v1/requests/"
        if path.startswith(prefix):
            status, body = self.runtime.request_status(unquote(path[len(prefix):]))
            self._send(status, body)
            return
        self._send(HTTPStatus.NOT_FOUND, {"error_code": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if urlparse(self.path).path != "/internal/v1/generate":
            self._send(HTTPStatus.NOT_FOUND, {"error_code": "not_found"})
            return
        try:
            payload = self._read_payload()
            status, body = self.runtime.generate(payload)
        except BridgeError as exc:
            self._send(exc.status, {"state": "not_started", "error_code": exc.code})
            return
        self._send(status, body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.runtime.config.token}"
        if hmac.compare_digest(supplied, expected):
            return True
        self._send(HTTPStatus.UNAUTHORIZED, {"error_code": "antigravity_bridge_unauthorized"})
        return False

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            raise BridgeError("antigravity_request_body_invalid", HTTPStatus.BAD_REQUEST)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BridgeError("antigravity_request_body_invalid", HTTPStatus.BAD_REQUEST) from exc
        if not isinstance(payload, dict):
            raise BridgeError("antigravity_request_body_invalid", HTTPStatus.BAD_REQUEST)
        return payload

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args) -> None:
        return


def config_from_env() -> BridgeConfig:
    return BridgeConfig(
        slot_id=os.environ["ANTIGRAVITY_SLOT_ID"],
        token=os.environ["ANTIGRAVITY_BRIDGE_TOKEN"],
        agy_bin=Path(os.environ.get("ANTIGRAVITY_CLI_BIN", "~/.local/bin/agy")).expanduser(),
        ledger_path=Path(os.environ["ANTIGRAVITY_LEDGER_PATH"]),
        ledger_key=os.environ["ANTIGRAVITY_LEDGER_KEY"],
        max_timeout_seconds=int(os.environ.get("ANTIGRAVITY_MAX_TIMEOUT_SECONDS", "180")),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.runtime = AntigravityRuntime(config_from_env())  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
