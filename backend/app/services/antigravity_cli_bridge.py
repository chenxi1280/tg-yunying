from __future__ import annotations

import fcntl
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from app.ai_gateway import AiGenerationResult, AiUsage, parse_draft_candidates
from app.config import Settings, get_settings


ANTIGRAVITY_STDERR_LIMIT = 300


class AntigravityCliUnavailable(RuntimeError):
    pass


def clean_markdown_json_fences(text: str) -> str:
    cleaned = (text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return cleaned


class AntigravityCliBridge:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        runner: Callable = subprocess.run,
    ) -> None:
        self._settings = settings or get_settings()
        self._runner = runner

    def generate(
        self,
        *,
        system_prompt: str = "",
        user_prompt: str,
        count: int = 1,
        model: str | None = None,
        effort: str | None = None,
        persona_set: list[str] | tuple[str, ...] | None = None,
    ) -> AiGenerationResult:
        if not self._settings.antigravity_cli_enabled:
            raise AntigravityCliUnavailable("antigravity_cli_disabled")
        lock_path = Path(self._settings.antigravity_cli_lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            self._acquire_lock(lock_file)
            return self._run_locked(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                count=count,
                model=model,
                effort=effort,
                persona_set=persona_set,
            )

    def generate_raw(
        self,
        *,
        prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        if not self._settings.antigravity_cli_enabled:
            raise AntigravityCliUnavailable("antigravity_cli_disabled")
        lock_path = Path(self._settings.antigravity_cli_lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            self._acquire_lock(lock_file)
            completed = self._execute_cli(prompt, model=model, effort=effort)
            if completed.returncode != 0:
                detail = str(completed.stderr or "")[:ANTIGRAVITY_STDERR_LIMIT].replace("\n", " ")
                raise AntigravityCliUnavailable(f"antigravity_cli_exit_{completed.returncode}: {detail}")
            return str(completed.stdout or "").strip()

    def _acquire_lock(self, lock_file) -> None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AntigravityCliUnavailable("antigravity_cli_capacity_busy") from exc

    def _run_locked(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        count: int,
        model: str | None = None,
        effort: str | None = None,
        persona_set: list[str] | tuple[str, ...] | None = None,
    ) -> AiGenerationResult:
        combined_prompt = f"{system_prompt}\n\n{user_prompt}".strip() if system_prompt else user_prompt
        completed = self._execute_cli(combined_prompt, model=model, effort=effort)
        return self._parse_result(completed, count, persona_set=persona_set)

    def _execute_cli(
        self,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> subprocess.CompletedProcess:
        target_model = model or self._settings.antigravity_cli_model
        target_effort = effort or self._settings.antigravity_cli_effort
        cmd = [
            self._settings.antigravity_cli_bin,
            "-p", prompt,
            "--model", target_model,
            "--effort", target_effort,
            "--output-format", "text",
        ]
        try:
            return self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._settings.antigravity_cli_timeout_seconds,
                shell=False,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            code = "binary_missing" if isinstance(exc, FileNotFoundError) else "timeout"
            raise AntigravityCliUnavailable(f"antigravity_cli_{code}") from exc

    def _parse_result(
        self,
        completed: subprocess.CompletedProcess,
        count: int,
        *,
        persona_set: list[str] | tuple[str, ...] | None = None,
    ) -> AiGenerationResult:
        if completed.returncode != 0:
            detail = str(completed.stderr or "")[:ANTIGRAVITY_STDERR_LIMIT].replace("\n", " ")
            raise AntigravityCliUnavailable(f"antigravity_cli_exit_{completed.returncode}: {detail}")
        raw_text = str(completed.stdout or "").strip()
        if not raw_text:
            raise AntigravityCliUnavailable("antigravity_cli_empty_text")
        cleaned = clean_markdown_json_fences(raw_text)
        personas = list(persona_set) if persona_set else ["普通群友"]
        try:
            candidates = parse_draft_candidates(cleaned, count=count, persona_set=personas)
        except Exception as exc:
            raise AntigravityCliUnavailable(f"antigravity_cli_parse_error: {exc}") from exc
        if not candidates:
            raise AntigravityCliUnavailable("antigravity_cli_empty_candidates")
        return AiGenerationResult(candidates=candidates, usage=AiUsage(total_tokens=len(cleaned)))
