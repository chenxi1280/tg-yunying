from __future__ import annotations

import base64
import hashlib
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import subprocess

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.workers.authorization_dr_kms import WrappedDek


KEY_AAD = b"tg-authorization-standby-2-dek-v1"
OBJECT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


class FileDekProtector:
    def __init__(self, key_file: str):
        encoded = Path(key_file).read_text().strip()
        self._key = base64.b64decode(encoded, validate=True)
        if len(self._key) != 32:
            raise ValueError("MY recovery key must decode to exactly 32 bytes")
        self._version = f"ssh-key-{hashlib.sha256(self._key).hexdigest()[:16]}"

    @property
    def key_ref(self) -> str:
        return self._version

    def wrap(self, plaintext: bytes) -> WrappedDek:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, KEY_AAD)
        payload = base64.b64encode(nonce + ciphertext).decode()
        return WrappedDek(payload, self._version)

    def unwrap(self, ciphertext: str) -> bytes:
        payload = base64.b64decode(ciphertext, validate=True)
        return AESGCM(self._key).decrypt(payload[:12], payload[12:], KEY_AAD)


class SshMirrorObjectSnapshotStore:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        identity_file: str,
        known_hosts_file: str,
        remote_dir: str,
        runner=subprocess.run,
    ):
        if not host or not user or port < 1:
            raise ValueError("SSH mirror endpoint is incomplete")
        base = PurePosixPath(remote_dir)
        if not base.is_absolute() or ".." in base.parts:
            raise ValueError("SSH mirror directory must be an absolute normalized path")
        self._target = f"{user}@{host}"
        self._remote_dir = base
        self._runner = runner
        self._ssh = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts_file}",
            "-o", "ConnectTimeout=15",
            "-i", identity_file,
            "-p", str(port),
        ]

    def put_immutable(self, object_key: str, payload: bytes) -> str:
        target = self._remote_path(object_key)
        command = self._write_command(target)
        result = self._run(command, payload)
        digest = hashlib.sha256(payload).hexdigest()
        if result.stdout.decode().strip() != digest:
            raise RuntimeError("SSH mirror write digest mismatch")
        return f"sha256-{digest}"

    def read(self, object_key: str) -> bytes:
        target = self._remote_path(object_key)
        return self._run(f"cat -- {shlex.quote(str(target))}", None).stdout

    def exists(self, object_key: str) -> bool:
        target = shlex.quote(str(self._remote_path(object_key)))
        command = f"if test -f {target}; then printf 1; elif test -e {target}; then exit 2; else printf 0; fi"
        return self._run(command, None).stdout == b"1"

    def _remote_path(self, object_key: str) -> PurePosixPath:
        if not OBJECT_KEY_PATTERN.fullmatch(object_key):
            raise ValueError("SSH mirror object key contains unsupported characters")
        relative = PurePosixPath(object_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("SSH mirror object key must be relative and normalized")
        return self._remote_dir / relative

    def _run(self, command: str, payload: bytes | None) -> subprocess.CompletedProcess:
        result = self._runner(
            [*self._ssh, self._target, command],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"SSH mirror operation failed: {detail}")
        return result

    @staticmethod
    def _write_command(target: PurePosixPath) -> str:
        quoted_target = shlex.quote(str(target))
        quoted_parent = shlex.quote(str(target.parent))
        return (
            "set -eu; umask 077; "
            f"mkdir -p -- {quoted_parent}; target={quoted_target}; "
            'test ! -e "$target"; tmp="${target}.tmp.$$"; '
            'trap \'rm -f -- "$tmp"\' EXIT; cat >"$tmp"; chmod 600 "$tmp"; '
            'sync "$tmp"; ln "$tmp" "$target"; rm -f -- "$tmp"; trap - EXIT; '
            'sha256sum "$target" | cut -d" " -f1'
        )


__all__ = ["FileDekProtector", "SshMirrorObjectSnapshotStore"]
