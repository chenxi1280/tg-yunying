"""Bound HTTP I/O by an absolute deadline and reap the dedicated child process."""
import base64
import io
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

from app.ai_transport_errors import AiProviderResultUnknown


TRANSPORT_POLICY_REVISION = "isolated_http_deadline_tracked_v2"
MAX_REAP_RESERVE_SECONDS = 0.25
REAP_RESERVE_FRACTION = 0.25


def hard_deadline_options(deadline: float | None) -> dict:
    return {"request_deadline": deadline} if deadline is not None else {}


class AiHttpCallNotStarted(TimeoutError):
    pass


class AiHttpResultUnknown(AiProviderResultUnknown):
    def __init__(self, reason: str, *, process_id: int, termination_confirmed: bool):
        super().__init__(f"{reason}:pid={process_id}:termination_confirmed={termination_confirmed}")
        self.local_worker_pid = process_id
        self.local_termination_confirmed = termination_confirmed


def read_http(request, *, timeout: float, request_deadline: float | None = None) -> bytes:
    if request_deadline is None:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    if not math.isfinite(request_deadline) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ai_http_deadline_invalid")
    spec = {"url": request.full_url, "method": request.get_method(), "headers": dict(request.header_items()),
            "body": base64.b64encode(request.data).decode("ascii") if request.data is not None else None,
            "timeout": timeout}
    raw = json.dumps(spec).encode("utf-8")
    remaining = request_deadline - time.monotonic()
    if remaining <= 0:
        raise AiHttpCallNotStarted("ai_http_deadline_exhausted_before_call")
    reserve = min(MAX_REAP_RESERVE_SECONDS, remaining * REAP_RESERVE_FRACTION)
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(Path(__file__).with_name("ai_http_worker.py"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
    )
    try:
        output = _exchange(process, raw, deadline=request_deadline, reserve=reserve)
        body = _decode_response(output, request=request, process=process)
        if time.monotonic() > request_deadline:
            raise AiHttpResultUnknown("ai_http_decode_after_deadline", process_id=process.pid, termination_confirmed=True)
        return body
    finally:
        _close_pipes(process)


def _close_pipes(process) -> None:
    errors = []
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except Exception as exc:
            errors.append(exc)
    if errors:
        confirmed = process.poll() is not None
        reason = "ai_http_pipe_cleanup_unproven" if confirmed else "local_http_termination_unproven"
        raise AiHttpResultUnknown(reason, process_id=process.pid, termination_confirmed=confirmed) from errors[0]


def _exchange(process, raw: bytes, *, deadline: float, reserve: float) -> bytes:
    try:
        output, _stderr = process.communicate(raw, timeout=max(0, deadline - reserve - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        confirmed = _terminate(process, deadline=deadline)
        reason = "ai_http_total_deadline_unknown" if confirmed else "local_http_termination_unproven"
        raise AiHttpResultUnknown(reason, process_id=process.pid, termination_confirmed=confirmed) from exc
    except Exception as exc:
        confirmed = _terminate(process, deadline=deadline)
        reason = "ai_http_exchange_unproven" if confirmed else "local_http_termination_unproven"
        raise AiHttpResultUnknown(reason, process_id=process.pid, termination_confirmed=confirmed) from exc
    except BaseException:
        _terminate(process, deadline=deadline)
        raise
    if process.returncode != 0 or time.monotonic() > deadline:
        raise AiHttpResultUnknown(f"ai_http_exchange_unproven:exit={process.returncode}", process_id=process.pid, termination_confirmed=True)
    return output


def _terminate(process, *, deadline: float) -> bool:
    try:
        if process.poll() is None:
            process.kill()
    except OSError as exc:
        raise AiHttpResultUnknown("local_http_termination_unproven", process_id=process.pid, termination_confirmed=False) from exc
    try:
        process.communicate(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return process.poll() is not None
    except Exception as exc:
        confirmed = process.poll() is not None
        reason = "ai_http_reap_unproven" if confirmed else "local_http_termination_unproven"
        raise AiHttpResultUnknown(reason, process_id=process.pid, termination_confirmed=confirmed) from exc
    return process.poll() is not None


def _decode_response(raw: bytes, *, request, process) -> bytes:
    try:
        result = json.loads(raw)
        if result.get("error") == "connection_not_started":
            raise urllib.error.URLError(ConnectionRefusedError("ai_http_connection_not_started"))
        if result.get("error"):
            reason = f"{result['error']}:{result.get('error_type', 'unavailable')}"
            raise AiHttpResultUnknown(reason, process_id=process.pid, termination_confirmed=True)
        body = base64.b64decode(result["body"], validate=True)
        status = int(result["status"])
        headers = Message()
        for name, value in result["headers"]:
            headers[name] = value
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AiHttpResultUnknown("ai_http_worker_protocol_unproven", process_id=process.pid, termination_confirmed=True) from exc
    if result.get("http_error") or status >= 400:
        raise urllib.error.HTTPError(request.full_url, status, "AI HTTP error", headers, io.BytesIO(body))
    return body
