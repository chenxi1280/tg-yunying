#!/usr/bin/env python3
"""Antigravity CLI OpenAI-Compatible HTTP Bridge Server.

Exposes a standard OpenAI /v1/chat/completions endpoint backed by Antigravity CLI (agy).
Can be deployed as a systemd service or background daemon on host servers.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("antigravity_bridge")


def clean_markdown_fences(text: str) -> str:
    cleaned = (text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return cleaned


class AntigravityBridgeHandler(BaseHTTPRequestHandler):
    server_version = "AntigravityBridge/1.0"

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok", "service": "antigravity-cli-bridge"})
            return
        if self.path.startswith("/v1/models"):
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "gemini-3.5-flash", "object": "model", "owned_by": "google-antigravity"},
                    {"id": "gemini-3.7-flash", "object": "model", "owned_by": "google-antigravity"},
                    {"id": "gemini-3.6-flash", "object": "model", "owned_by": "google-antigravity"},
                    {"id": "gemini-3.1-pro", "object": "model", "owned_by": "google-antigravity"},
                ]
            })
            return
        self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        if not (self.path.startswith("/v1/chat/completions") or self.path == "/chat/completions"):
            self._send_json(404, {"error": "Endpoint not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._send_json(400, {"error": "Missing or empty request body"})
            return

        raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"Invalid JSON body: {exc}"})
            return

        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            self._send_json(400, {"error": "messages must be a list"})
            return

        # Map model name
        default_model = os.getenv("ANTIGRAVITY_CLI_MODEL", "gemini-3.5-flash")
        requested_model = str(payload.get("model") or default_model).strip()
        model = self._map_model(requested_model)
        effort = str(payload.get("effort") or os.getenv("ANTIGRAVITY_CLI_EFFORT", "medium")).strip()

        # Construct prompt
        system_parts = []
        user_parts = []
        for msg in messages:
            role = str(msg.get("role") or "").lower()
            content = str(msg.get("content") or "")
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                user_parts.append(f"{role}: {content}" if len(messages) > 2 else content)

        system_prompt = "\n".join(system_parts).strip()
        user_prompt = "\n".join(user_parts).strip()
        combined_prompt = f"{system_prompt}\n\n{user_prompt}".strip() if system_prompt else user_prompt

        logger.info("Executing agy prompt (model=%s, prompt_len=%d)", model, len(combined_prompt))
        started_at = time.monotonic()

        try:
            output = self._call_agy(combined_prompt, model=model, effort=effort)
            duration_ms = round((time.monotonic() - started_at) * 1000)
            logger.info("agy succeeded in %dms (output_len=%d)", duration_ms, len(output))
            cleaned_output = clean_markdown_fences(output)

            response_data = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": cleaned_output,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(combined_prompt),
                    "completion_tokens": len(cleaned_output),
                    "total_tokens": len(combined_prompt) + len(cleaned_output),
                },
            }
            self._send_json(200, response_data)
        except Exception as exc:
            logger.exception("agy execution failed: %s", exc)
            self._send_json(500, {"error": {"message": str(exc), "type": "antigravity_error"}})

    def _map_model(self, requested: str) -> str:
        req_lower = requested.lower()
        if "3.7" in req_lower:
            return "gemini-3.7-flash"
        if "3.6" in req_lower:
            return "gemini-3.6-flash"
        if "3.1" in req_lower or "pro" in req_lower:
            return "gemini-3.1-pro"
        return "gemini-3.5-flash"

    def _call_agy(self, prompt: str, *, model: str, effort: str) -> str:
        agy_bin = os.getenv("ANTIGRAVITY_CLI_BIN", "/root/.local/bin/agy")
        if not os.path.exists(agy_bin):
            # fallback to user local bin
            user_bin = os.path.expanduser("~/.local/bin/agy")
            if os.path.exists(user_bin):
                agy_bin = user_bin

        lock_path = Path(os.getenv("ANTIGRAVITY_CLI_LOCK_PATH", "/tmp/tgyunying-antigravity-cli.lock"))
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        timeout = int(os.getenv("ANTIGRAVITY_CLI_TIMEOUT_SECONDS", "90"))
        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("antigravity_cli_capacity_busy") from exc

            try:
                completed = subprocess.run(
                    [agy_bin, "-p", prompt, "--model", model, "--effort", effort, "--output-format", "text"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        if completed.returncode != 0:
            err = (completed.stderr or "").strip()
            raise RuntimeError(f"agy exit {completed.returncode}: {err[:300]}")
        return (completed.stdout or "").strip()

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)


def main():
    parser = argparse.ArgumentParser(description="Antigravity CLI OpenAI-Compatible HTTP Bridge")
    parser.add_argument("--host", default=os.getenv("ANTIGRAVITY_BRIDGE_HOST", "127.0.0.1"), help="Host to bind")
    parser.add_argument("--port", type=int, default=int(os.getenv("ANTIGRAVITY_BRIDGE_PORT", "18099")), help="Port to bind")
    args = parser.parse_args()

    server_address = (args.host, args.port)
    httpd = ThreadingHTTPServer(server_address, AntigravityBridgeHandler)
    logger.info("Starting Antigravity CLI Bridge on http://%s:%d", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Antigravity Bridge...")
        httpd.server_close()


if __name__ == "__main__":
    main()
