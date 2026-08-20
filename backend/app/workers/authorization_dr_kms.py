from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol


ENCRYPTION_CONTEXT = {"purpose": "tg-authorization-standby-2"}


@dataclass(frozen=True)
class WrappedDek:
    ciphertext: str
    key_version: str


class DekProtector(Protocol):
    @property
    def key_ref(self) -> str: ...

    def wrap(self, plaintext: bytes) -> WrappedDek: ...

    def unwrap(self, ciphertext: str) -> bytes: ...


class AlibabaKmsDekProtector:
    def __init__(
        self,
        *,
        endpoint: str,
        region_id: str,
        access_key_id: str,
        access_key_secret: str,
        key_id: str,
        client=None,
    ):
        self._key_id = key_id
        self._client = client or _create_client(
            endpoint=endpoint,
            region_id=region_id,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )

    @property
    def key_ref(self) -> str:
        return self._key_id

    def wrap(self, plaintext: bytes) -> WrappedDek:
        from alibabacloud_kms20160120 import models as kms_models

        request = kms_models.EncryptRequest(
            key_id=self._key_id,
            plaintext=base64.b64encode(plaintext).decode(),
            encryption_context=ENCRYPTION_CONTEXT,
        )
        body = self._client.encrypt(request).body
        if not body.ciphertext_blob or not body.key_version_id:
            raise RuntimeError("Alibaba KMS encrypt response is incomplete")
        return WrappedDek(body.ciphertext_blob, body.key_version_id)

    def unwrap(self, ciphertext: str) -> bytes:
        from alibabacloud_kms20160120 import models as kms_models

        request = kms_models.DecryptRequest(
            ciphertext_blob=ciphertext,
            encryption_context=ENCRYPTION_CONTEXT,
        )
        body = self._client.decrypt(request).body
        if not body.plaintext:
            raise RuntimeError("Alibaba KMS decrypt response is incomplete")
        return base64.b64decode(body.plaintext)


def _create_client(*, endpoint: str, region_id: str, access_key_id: str, access_key_secret: str):
    from alibabacloud_kms20160120.client import Client
    from alibabacloud_tea_openapi import models as open_api_models

    config = open_api_models.Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        region_id=region_id,
    )
    config.endpoint = endpoint
    return Client(config)


__all__ = ["AlibabaKmsDekProtector", "DekProtector", "WrappedDek"]
