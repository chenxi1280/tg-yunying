from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
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


@dataclass
class _ClientCacheEntry:
    client: Any
    created_at: float
    last_used_at: float


class TelethonClientLifecycle:
    """Owns the process-wide Telethon event loop and connected client cache."""

    _loop: asyncio.AbstractEventLoop | None = None
    _loop_thread: threading.Thread | None = None
    _cache: dict[tuple[int, str, str], _ClientCacheEntry] = {}
    _lock: threading.Lock = threading.Lock()

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

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

    def run(self, coro):
        loop = self.get_or_create_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=self.settings.telethon_operation_timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            raise

    def new_client(
        self,
        credentials: DeveloperAppCredentialsLike,
        raw_session: str | None = None,
        client_metadata: Mapping[str, str] | None = None,
    ) -> Any:
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
    ) -> Any:
        await self.prune_idle_clients()
        cache_key = self._cache_key(credentials, raw_session, client_metadata)
        now = time.monotonic()

        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is not None:
                entry.last_used_at = now
                client = entry.client
            else:
                client = None

        if client is not None:
            try:
                if client.is_connected():
                    return client
            except Exception:
                pass
            await self._disconnect_quietly(client)
            with self._lock:
                current = self._cache.get(cache_key)
                if current and current.client is client:
                    self._cache.pop(cache_key, None)

        client = self.new_client(credentials, raw_session, client_metadata)
        try:
            await asyncio.wait_for(client.connect(), timeout=self.settings.telethon_client_connect_timeout_seconds)
        except Exception:
            await self._disconnect_quietly(client)
            raise
        await self.remember_connected_client(credentials, raw_session, client, client_metadata=client_metadata)
        await self.enforce_cache_limit()
        return client

    async def remember_connected_client(
        self,
        credentials: DeveloperAppCredentialsLike,
        raw_session: str,
        client: Any,
        *,
        client_metadata: Mapping[str, str] | None = None,
    ) -> None:
        cache_key = self._cache_key(credentials, raw_session, client_metadata)
        now = time.monotonic()
        with self._lock:
            self._cache[cache_key] = _ClientCacheEntry(client=client, created_at=now, last_used_at=now)

    async def invalidate_client(self, credentials: DeveloperAppCredentialsLike, raw_session: str) -> None:
        cache_key = self._cache_key(credentials, raw_session)
        with self._lock:
            entry = self._cache.pop(cache_key, None)
        if entry is not None:
            await self._disconnect_quietly(entry.client)

    async def prune_idle_clients(self) -> int:
        idle_seconds = self.settings.telethon_client_idle_seconds
        if idle_seconds <= 0:
            return 0
        cutoff = time.monotonic() - idle_seconds
        expired: list[_ClientCacheEntry] = []
        with self._lock:
            for cache_key, entry in list(self._cache.items()):
                if entry.last_used_at <= cutoff:
                    expired.append(self._cache.pop(cache_key))
        await self._disconnect_entries(expired)
        return len(expired)

    async def enforce_cache_limit(self) -> int:
        max_clients = self.settings.telethon_client_cache_size
        if max_clients <= 0:
            return 0
        evicted: list[_ClientCacheEntry] = []
        with self._lock:
            while len(self._cache) > max_clients:
                oldest_key = min(self._cache, key=lambda item: self._cache[item].last_used_at)
                evicted.append(self._cache.pop(oldest_key))
        await self._disconnect_entries(evicted)
        return len(evicted)

    def _cache_key(
        self,
        credentials: DeveloperAppCredentialsLike,
        raw_session: str,
        client_metadata: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str, str]:
        return (int(credentials.api_id), raw_session, self._proxy_fingerprint(credentials), self._client_metadata_fingerprint(client_metadata))

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
        host = getattr(credentials, "proxy_host", "") or ""
        port = getattr(credentials, "proxy_port", None)
        if not host or not port:
            return None
        protocol = (getattr(credentials, "proxy_protocol", "") or "socks5").lower()
        TelethonClientLifecycle._validate_proxy_protocol(protocol)
        try:
            import socks
        except ImportError as exc:
            raise RuntimeError("PySocks package is required for Telegram proxy support") from exc
        username = getattr(credentials, "proxy_username", "") or None
        password = getattr(credentials, "proxy_password", "") or None
        return (TelethonClientLifecycle._proxy_type(socks, protocol), host, int(port), True, username, password)

    @staticmethod
    def _validate_proxy_protocol(protocol: str) -> None:
        if protocol not in {"socks5", "socks4", "http", "https"}:
            raise ValueError(f"不支持的代理协议：{protocol}")

    @staticmethod
    def _proxy_type(socks_module, protocol: str):
        if protocol == "socks5":
            return socks_module.SOCKS5
        if protocol == "socks4":
            return socks_module.SOCKS4
        if protocol in {"http", "https"}:
            return socks_module.HTTP
        raise ValueError(f"不支持的代理协议：{protocol}")

    @classmethod
    async def shutdown_all(cls) -> int:
        with cls._lock:
            entries = list(cls._cache.values())
            cls._cache.clear()
        return await cls._disconnect_entries(entries)

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
            result = client.disconnect()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            return


def shutdown_telethon_lifecycle(timeout_seconds: float | None = None) -> int:
    lifecycle = TelethonClientLifecycle()
    loop = TelethonClientLifecycle._loop
    if loop is None or loop.is_closed():
        return 0
    future = asyncio.run_coroutine_threadsafe(TelethonClientLifecycle.shutdown_all(), loop)
    disconnected = future.result(timeout=timeout_seconds or lifecycle.settings.telethon_operation_timeout_seconds)
    loop.call_soon_threadsafe(loop.stop)
    thread = TelethonClientLifecycle._loop_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout_seconds or 5)
    with TelethonClientLifecycle._lock:
        TelethonClientLifecycle._loop = None
        TelethonClientLifecycle._loop_thread = None
    return disconnected
