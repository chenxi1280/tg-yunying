from __future__ import annotations

import asyncio
import inspect
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Protocol

from .config import Settings, get_settings


class DeveloperAppCredentialsLike(Protocol):
    api_id: int
    api_hash: str
    proxy_id: int | None
    proxy_protocol: str
    proxy_host: str
    proxy_port: int | None
    proxy_username: str
    proxy_password: str


class TelethonOperationTimeout(TimeoutError):
    def __init__(self, *, transport_termination_acknowledged: bool,
                 termination_event: threading.Event | None = None) -> None:
        super().__init__("telethon_operation_timeout")
        self.transport_termination_acknowledged = (
            transport_termination_acknowledged
        )
        self.termination_event = termination_event


@dataclass
class _ClientCacheEntry:
    client: Any
    created_at: float
    last_used_at: float
    credential_fingerprint: str = ""
    metadata_fingerprint: str = ""
    ready: bool = True


class TelethonClientLifecycle:
    """Owns the process-wide Telethon event loop and connected client cache."""

    _loop: asyncio.AbstractEventLoop | None = None
    _loop_thread: threading.Thread | None = None
    _cache: dict[tuple[int, str, str, str], _ClientCacheEntry] = {}
    _creating: dict[tuple, Future] = {}
    _active_keys: dict[tuple, int] = {}
    _lock: threading.Lock = threading.Lock()
    _runtime_role: str = "all"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @classmethod
    def connected_client_count(cls) -> int:
        with cls._lock:
            return sum(bool(entry.client.is_connected()) for entry in cls._cache.values())

    @classmethod
    def set_runtime_role(cls, role: str) -> None:
        cls._runtime_role = str(role or "all").strip().lower()

    @classmethod
    def _assert_remote_io_allowed(cls) -> None:
        if cls._runtime_role == "planner":
            raise RuntimeError("planner_remote_io_forbidden")

    @classmethod
    def get_or_create_loop(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is None or cls._loop.is_closed():
                cls._loop = asyncio.new_event_loop()
                cls._loop_thread = threading.Thread(
                    target=cls._loop.run_forever,
                    name="tg-yunying-telethon-loop",
                    daemon=True,
                )
                cls._loop_thread.start()
            return cls._loop

    def run(self, coro, *, timeout_seconds: float | None = None):
        try:
            self._assert_remote_io_allowed()
        except RuntimeError:
            if inspect.iscoroutine(coro):
                coro.close()
            raise
        loop = self.get_or_create_loop()
        timeout = self.settings.telethon_operation_timeout_seconds if timeout_seconds is None else timeout_seconds
        terminated = threading.Event()

        async def observed_runner():
            try:
                return await coro
            finally:
                terminated.set()

        future = asyncio.run_coroutine_threadsafe(observed_runner(), loop)
        grace = min(0.1, max(0.001, float(timeout) / 10))
        result_timeout = max(0.001, float(timeout) - grace)
        try:
            return future.result(timeout=result_timeout)
        except FutureTimeoutError:
            future.cancel()
            acknowledged = terminated.wait(timeout=grace)
            raise TelethonOperationTimeout(
                transport_termination_acknowledged=acknowledged,
                termination_event=terminated,
            ) from None

    def new_client(
        self,
        credentials: DeveloperAppCredentialsLike,
        raw_session: str | None = None,
        client_metadata: Mapping[str, str] | None = None,
    ) -> Any:
        self._assert_remote_io_allowed()
        if self.settings.telegram_owner_mode == "client":
            raise RuntimeError("telegram_client_requires_connection_owner")
        self._proxy_config(credentials)
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise RuntimeError("Telethon package is not installed") from exc
        from telethon.sessions import StringSession

        return TelegramClient(
            StringSession(raw_session or ""),
            int(credentials.api_id),
            credentials.api_hash,
            proxy=self._proxy_config(credentials),
            **self._client_metadata_options(client_metadata),
        )

    async def get_or_create_client(
        self,
        credentials: DeveloperAppCredentialsLike,
        raw_session: str,
        client_metadata: Mapping[str, str] | None = None,
        *,
        connect_timeout_seconds: float | None = None,
    ) -> Any:
        self._assert_remote_io_allowed()
        if self.settings.telegram_owner_mode == "client":
            raise RuntimeError("telegram_client_requires_connection_owner")
        self._proxy_config(credentials)
        key = self._cache_key(credentials, raw_session, client_metadata)
        with self._lock:
            flight = self._creating.get(key)
            creator = flight is None
            if creator:
                flight = Future()
                self._creating[key] = flight
        if not creator:
            return await self._await_creation(flight, credentials, raw_session=raw_session, metadata=client_metadata)
        try:
            result = await self._connect_or_reuse(
                credentials, raw_session, client_metadata,
                connect_timeout_seconds=connect_timeout_seconds,
            )
            flight.set_result((result, self._credential_fingerprint(credentials),
                               self._client_metadata_fingerprint(client_metadata)))
            return result
        except BaseException as exc:
            flight.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._creating.pop(key, None)

    async def _await_creation(self, flight, credentials, *, raw_session, metadata):
        result = await asyncio.shield(asyncio.wrap_future(flight))
        if result is None:  # Eviction completed; acquire a new shared creation flight.
            return await self.get_or_create_client(credentials, raw_session, metadata)
        client, credential_fingerprint, metadata_fingerprint = result
        if credential_fingerprint != self._credential_fingerprint(credentials):
            raise ValueError("telegram_authorization_app_identity_conflict")
        requested = self._client_metadata_fingerprint(metadata)
        if requested.strip("|") and requested != metadata_fingerprint:
            raise ValueError("telegram_metadata_change_requires_owner_serialization")
        return client

    async def _connect_or_reuse(self, credentials, raw_session, client_metadata, *, connect_timeout_seconds):
        key = self._cache_key(credentials, raw_session)
        with self._lock:
            entry = self._cache.get(key)
        if entry is not None:
            if entry.credential_fingerprint != self._credential_fingerprint(credentials):
                raise ValueError("telegram_authorization_app_identity_conflict")
            metadata = self._client_metadata_fingerprint(client_metadata)
            unchanged = not metadata.strip("|") or metadata == entry.metadata_fingerprint
            if entry.ready and unchanged and entry.client.is_connected():
                entry.last_used_at = time.monotonic()
                return entry.client
            await self._remove_after_disconnect(key, entry)
        await self.prune_idle_clients()
        return await self._connect_new(
            credentials, raw_session, client_metadata,
            connect_timeout_seconds=connect_timeout_seconds,
        )

    async def _connect_new(self, credentials, raw_session, client_metadata, *, connect_timeout_seconds):
        timeout = (self.settings.telethon_client_connect_timeout_seconds
                   if connect_timeout_seconds is None else connect_timeout_seconds)
        if timeout <= 0:
            raise ValueError("telethon_connect_timeout_must_be_positive")
        client = self.new_client(credentials, raw_session, client_metadata)
        key = self._cache_key(credentials, raw_session)
        entry = self._entry(credentials, client, client_metadata, ready=False)
        with self._lock:
            self._cache[key] = entry
        try:
            await asyncio.wait_for(client.connect(), timeout=timeout)
        except BaseException:
            await self._remove_after_disconnect(key, entry)
            raise
        entry.ready = True
        await self.enforce_cache_limit()
        return client

    async def _remove_after_disconnect(self, key, entry):
        entry.ready = False
        await self._disconnect(entry.client)
        with self._lock:
            if self._cache.get(key) is entry:
                self._cache.pop(key)

    def _entry(self, credentials, client, metadata, *, ready=True):
        now = time.monotonic()
        return _ClientCacheEntry(client, now, now, self._credential_fingerprint(credentials),
                                 self._client_metadata_fingerprint(metadata), ready)

    @staticmethod
    def _credential_fingerprint(credentials):
        import hashlib

        value = f"{credentials.api_id}:{credentials.api_hash}"
        return hashlib.sha256(value.encode()).hexdigest()

    async def remember_connected_client(
        self,
        credentials: DeveloperAppCredentialsLike,
        raw_session: str,
        client: Any,
        *,
        client_metadata: Mapping[str, str] | None = None,
    ) -> None:
        key = self._cache_key(credentials, raw_session)
        with self._lock:
            prior = self._cache.get(key)
        if prior is not None and prior.client is not client:
            await self._remove_after_disconnect(key, prior)
        with self._lock:
            self._cache[key] = self._entry(credentials, client, client_metadata)

    async def invalidate_client(self, credentials, raw_session) -> int:
        key = self._cache_key(credentials, raw_session)
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            return 0
        await self._remove_after_disconnect(key, entry)
        return 1

    async def prune_idle_clients(self) -> int:
        idle = self.settings.telethon_client_idle_seconds
        if idle <= 0:
            return 0
        cutoff = time.monotonic() - idle
        with self._lock:
            expired = [(key, entry) for key, entry in self._cache.items()
                       if entry.last_used_at <= cutoff and key not in self._active_keys
                       and key not in self._creating]
        return await self._evict_unused(expired)

    async def enforce_cache_limit(self) -> int:
        limit = self.settings.telethon_client_cache_size
        if limit <= 0:
            return 0
        with self._lock:
            candidates = sorted(((key, entry) for key, entry in self._cache.items()
                                 if key not in self._active_keys and key not in self._creating),
                                key=lambda row: row[1].last_used_at)
            evicted = candidates[:max(0, len(self._cache) - limit)]
        return await self._evict_unused(evicted)

    async def _evict_unused(self, candidates):
        removed = 0
        for key, entry in candidates:
            with self._lock:
                if (key in self._active_keys or key in self._creating
                        or self._cache.get(key) is not entry):
                    continue
                flight = Future()
                self._creating[key] = flight
            try:
                await self._remove_after_disconnect(key, entry)
                flight.set_result(None)
                removed += 1
            except BaseException as exc:
                flight.set_exception(exc)
                raise
            finally:
                with self._lock:
                    self._creating.pop(key, None)
        return removed

    @staticmethod
    def _cache_key(credentials, raw_session, client_metadata=None) -> tuple:
        from .telegram_owner.session_identity import authorization_identity

        return (0, authorization_identity(raw_session), "", "")

    @classmethod
    def pin_authorizations(cls, identities):
        with cls._lock:
            for identity in identities:
                key = (0, identity, "", "")
                cls._active_keys[key] = cls._active_keys.get(key, 0) + 1

    @classmethod
    def unpin_authorizations(cls, identities):
        with cls._lock:
            for identity in identities:
                key = (0, identity, "", "")
                count = cls._active_keys[key] - 1
                if count:
                    cls._active_keys[key] = count
                else:
                    cls._active_keys.pop(key)

    @staticmethod
    def _client_metadata_options(client_metadata: Mapping[str, str] | None) -> dict[str, str]:
        metadata = client_metadata or {}
        return {
            key: value
            for key in ("device_model", "system_version", "app_version", "lang_code", "system_lang_code")
            if (value := str(metadata.get(key) or "").strip())
        }

    @staticmethod
    def _client_metadata_fingerprint(client_metadata: Mapping[str, str] | None) -> str:
        metadata = client_metadata or {}
        keys = ("device_model", "system_version", "app_version", "platform", "lang_code", "system_lang_code", "client_identity_key")
        return "|".join(str(metadata.get(key) or "").strip() for key in keys)

    @staticmethod
    def _proxy_fingerprint(credentials: DeveloperAppCredentialsLike) -> str:
        proxy_id = getattr(credentials, "proxy_id", None)
        protocol = getattr(credentials, "proxy_protocol", "") or ""
        host = getattr(credentials, "proxy_host", "") or ""
        port = getattr(credentials, "proxy_port", None) or ""
        username = getattr(credentials, "proxy_username", "") or ""
        return f"{proxy_id}:{protocol}:{host}:{port}:{username}"

    @staticmethod
    def _proxy_config(credentials: DeveloperAppCredentialsLike):
        fields = ("proxy_id", "proxy_protocol", "proxy_host", "proxy_port", "proxy_username", "proxy_password")
        if any(getattr(credentials, field, None) for field in fields):
            raise ValueError("telegram_account_proxy_forbidden")
        return None

    @classmethod
    async def shutdown_all(cls) -> int:
        with cls._lock:
            entries = list(cls._cache.values())
            cls._cache.clear()
        return await cls._disconnect_entries(entries)

    @classmethod
    async def shutdown_all_strict(cls) -> int:
        with cls._lock:
            entries = list(cls._cache.items())
        failures: list[str] = []
        for _, entry in entries:
            try:
                await cls._disconnect(entry.client)
            except Exception as exc:  # noqa: BLE001 - strict drain reports all.
                failures.append(exc.__class__.__name__)
        if failures:
            raise RuntimeError(
                "Telethon disconnect failed: " + ",".join(failures)
            )
        with cls._lock:
            for cache_key, entry in entries:
                if cls._cache.get(cache_key) is entry:
                    cls._cache.pop(cache_key, None)
        return len(entries)

    @staticmethod
    async def _disconnect_entries(entries: list[_ClientCacheEntry]) -> int:
        disconnected = 0
        for entry in entries:
            await TelethonClientLifecycle._disconnect_quietly(entry.client)
            disconnected += 1
        return disconnected

    @staticmethod
    async def _disconnect_quietly(client: Any) -> None:
        try:
            await TelethonClientLifecycle._disconnect(client)
        except Exception:
            return

    @staticmethod
    async def _disconnect(client: Any) -> None:
        result = client.disconnect()
        if inspect.isawaitable(result):
            await result


def shutdown_telethon_lifecycle(timeout_seconds: float | None = None) -> int:
    return _shutdown_telethon_lifecycle(timeout_seconds, strict=False)


def shutdown_telethon_lifecycle_strict(
    timeout_seconds: float | None = None,
) -> int:
    return _shutdown_telethon_lifecycle(timeout_seconds, strict=True)


def _shutdown_telethon_lifecycle(
    timeout_seconds: float | None,
    *,
    strict: bool,
) -> int:
    lifecycle = TelethonClientLifecycle()
    loop = TelethonClientLifecycle._loop
    if loop is None or loop.is_closed():
        return 0
    operation = (
        TelethonClientLifecycle.shutdown_all_strict()
        if strict else TelethonClientLifecycle.shutdown_all()
    )
    future = asyncio.run_coroutine_threadsafe(operation, loop)
    timeout = timeout_seconds or lifecycle.settings.telethon_operation_timeout_seconds
    try:
        disconnected = future.result(timeout=timeout)
    except FutureTimeoutError:
        if strict:
            future.cancel()
        raise
    loop.call_soon_threadsafe(loop.stop)
    thread = TelethonClientLifecycle._loop_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout_seconds or 5)
    if strict and thread is not None and thread.is_alive():
        raise RuntimeError("Telethon event loop did not stop")
    with TelethonClientLifecycle._lock:
        TelethonClientLifecycle._loop = None
        TelethonClientLifecycle._loop_thread = None
    return disconnected
