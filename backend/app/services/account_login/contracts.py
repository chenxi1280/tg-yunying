from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeSourceSpec:
    url: str
    host: str
    uuid: str
    uuid_fingerprint: str
    uuid_hint: str


@dataclass(frozen=True)
class ParsedLoginLine:
    line_no: int
    phone: str
    phone_masked: str
    source: CodeSourceSpec


@dataclass(frozen=True)
class LoginMaterials:
    code: str
    password_2fa: str
    login_time: str
    last_fetch_time: str


class BatchLoginError(ValueError):
    def __init__(self, code: str, message: str, *, line_no: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line_no = line_no


__all__ = ["BatchLoginError", "CodeSourceSpec", "LoginMaterials", "ParsedLoginLine"]
