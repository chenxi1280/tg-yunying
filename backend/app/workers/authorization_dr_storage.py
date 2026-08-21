from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

import oss2

from app.workers.authorization_dr_kms import AlibabaKmsDekProtector, DekProtector
from app.workers.authorization_dr_ssh import FileDekProtector, SshMirrorObjectSnapshotStore


class ObjectSnapshotStore(Protocol):
    def put_immutable(self, object_key: str, payload: bytes) -> str: ...

    def read(self, object_key: str) -> bytes: ...

    def exists(self, object_key: str) -> bool: ...


class OssObjectSnapshotStore:
    def __init__(self, endpoint: str, bucket_name: str, access_key_id: str, access_key_secret: str):
        self.bucket_name = bucket_name
        self.bucket = oss2.Bucket(oss2.Auth(access_key_id, access_key_secret), endpoint, bucket_name)

    def put_immutable(self, object_key: str, payload: bytes) -> str:
        result = self.bucket.put_object(
            object_key,
            payload,
            headers={"x-oss-forbid-overwrite": "true"},
        )
        return str(result.headers.get("x-oss-version-id") or result.request_id)

    def read(self, object_key: str) -> bytes:
        return self.bucket.get_object(object_key).read()

    def exists(self, object_key: str) -> bool:
        return bool(self.bucket.object_exists(object_key))


@dataclass(frozen=True)
class StorageConfig:
    object_store: ObjectSnapshotStore
    dek_protector: DekProtector
    object_prefix: str
    copy_kind: str


def load_storage_config(required: Callable[[str], str]) -> StorageConfig:
    mode = required("MY_WAKE_STORAGE_MODE")
    if mode == "kms_oss":
        return _kms_oss_storage_config(required)
    if mode == "ssh_mirror":
        return _ssh_mirror_storage_config(required)
    raise ValueError("MY_WAKE_STORAGE_MODE must be kms_oss or ssh_mirror")


def _kms_oss_storage_config(required: Callable[[str], str]) -> StorageConfig:
    store = OssObjectSnapshotStore(
        required("MY_WAKE_OSS_ENDPOINT"),
        required("MY_WAKE_OSS_BUCKET"),
        required("MY_WAKE_OSS_ACCESS_KEY_ID"),
        required("MY_WAKE_OSS_ACCESS_KEY_SECRET"),
    )
    protector = AlibabaKmsDekProtector(
        endpoint=required("MY_WAKE_KMS_ENDPOINT"),
        region_id=required("MY_WAKE_KMS_REGION_ID"),
        access_key_id=required("MY_WAKE_KMS_ACCESS_KEY_ID"),
        access_key_secret=required("MY_WAKE_KMS_ACCESS_KEY_SECRET"),
        key_id=required("MY_WAKE_KMS_KEY_ID"),
    )
    return StorageConfig(store, protector, required("MY_WAKE_OSS_PREFIX"), "object_snapshot")


def _ssh_mirror_storage_config(required: Callable[[str], str]) -> StorageConfig:
    store = SshMirrorObjectSnapshotStore(
        host=required("MY_WAKE_SSH_HOST"),
        port=int(required("MY_WAKE_SSH_PORT")),
        user=required("MY_WAKE_SSH_USER"),
        identity_file=required("MY_WAKE_SSH_IDENTITY_FILE"),
        known_hosts_file=required("MY_WAKE_SSH_KNOWN_HOSTS_FILE"),
        remote_dir=required("MY_WAKE_SSH_REMOTE_DIR"),
    )
    protector = FileDekProtector(required("MY_WAKE_RECOVERY_KEY_FILE"))
    return StorageConfig(store, protector, required("MY_WAKE_SNAPSHOT_PREFIX"), "remote_ssh_snapshot")


__all__ = ["ObjectSnapshotStore", "StorageConfig", "load_storage_config"]
