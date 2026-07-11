from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from app.ai_gateway import AiGenerationResult, AiUsage, parse_draft_candidates
from app.config import Settings, get_settings


GROK_STDERR_LIMIT = 300


class GrokCliUnavailable(RuntimeError):
    pass


class GrokCliBridge:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        runner: Callable = subprocess.run,
    ) -> None:
        self._settings = settings or get_settings()
        self._runner = runner

    def generate(self, *, system_prompt: str, user_prompt: str, count: int) -> AiGenerationResult:
        if not self._settings.grok_cli_enabled:
            raise GrokCliUnavailable("grok_cli_disabled")
        lock_path = Path(self._settings.grok_cli_lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            self._acquire_lock(lock_file)
            return self._run_locked(system_prompt, user_prompt, count)

    def _acquire_lock(self, lock_file) -> None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GrokCliUnavailable("grok_cli_capacity_busy") from exc

    def _run_locked(self, system_prompt: str, user_prompt: str, count: int) -> AiGenerationResult:
        with tempfile.TemporaryDirectory(prefix="tgyunying-grok-") as workdir:
            subprocess.run(["git", "init", "-q", workdir], check=True, capture_output=True, text=True)
            try:
                completed = self._runner(
                    self._command(system_prompt, user_prompt, workdir),
                    capture_output=True,
                    text=True,
                    timeout=self._settings.grok_cli_timeout_seconds,
                    shell=False,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                code = "binary_missing" if isinstance(exc, FileNotFoundError) else "timeout"
                raise GrokCliUnavailable(f"grok_cli_{code}") from exc
        return self._parse_result(completed, count)

    def _command(self, system_prompt: str, user_prompt: str, workdir: str) -> list[str]:
        return [
            self._settings.grok_cli_bin,
            "--model", self._settings.grok_cli_model,
            "--single", user_prompt,
            "--verbatim",
            "--system-prompt-override", system_prompt,
            "--no-memory",
            "--no-subagents",
            "--disable-web-search",
            "--permission-mode", "dontAsk",
            "--cwd", workdir,
            "--output-format", "json",
        ]

    def _parse_result(self, completed, count: int) -> AiGenerationResult:
        if completed.returncode != 0:
            detail = str(completed.stderr or "")[:GROK_STDERR_LIMIT].replace("\n", " ")
            raise GrokCliUnavailable(f"grok_cli_exit_{completed.returncode}: {detail}")
        try:
            envelope = json.loads(str(completed.stdout or ""))
        except json.JSONDecodeError as exc:
            raise GrokCliUnavailable("grok_cli_invalid_envelope") from exc
        if str(envelope.get("stopReason") or "") != "EndTurn":
            raise GrokCliUnavailable("grok_cli_invalid_stop_reason")
        text = str(envelope.get("text") or "").strip()
        if not text:
            raise GrokCliUnavailable("grok_cli_empty_text")
        candidates = parse_draft_candidates(text, count=count, persona_set=["普通群友"])
        if not candidates:
            raise GrokCliUnavailable("grok_cli_empty_candidates")
        return AiGenerationResult(candidates=candidates, usage=AiUsage())
