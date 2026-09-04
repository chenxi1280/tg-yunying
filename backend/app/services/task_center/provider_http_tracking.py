"""Inject tracked transport into one gateway call, never into the shared singleton."""
from copy import copy, deepcopy
from dataclasses import dataclass
import hashlib
import json
import time
import urllib.error
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.ai_http_transport import AiHttpCallNotStarted
from app.ai_transport_errors import AiProviderResultUnknown
from .provider_http_exchanges import ExchangeScope, receive_exchange, start_exchange


@dataclass(frozen=True)
class TrackedProviderHttp:
    session_factory: object
    scope: ExchangeScope
    transport: object
    chain_id: str

    def __call__(self, request, *, timeout, request_deadline=None):
        if request_deadline is None:
            raise ValueError("provider_http_hard_deadline_missing")
        if time.monotonic() >= request_deadline:
            raise AiHttpCallNotStarted("provider_http_deadline_before_record")
        digest = hashlib.sha256(json.dumps({"url": request.full_url, "method": request.get_method(),
            "body_hash": hashlib.sha256(request.data or b"").hexdigest()}, sort_keys=True).encode()).hexdigest()
        exchange_id = start_exchange(self.session_factory, self.scope, chain_id=self.chain_id, request_hash=digest)
        try:
            body = self.transport(request, timeout=timeout, request_deadline=request_deadline)
        except urllib.error.HTTPError as exc:
            self._receive(exchange_id, outcome="response_received", http_status=exc.code, error_code="HTTPError")
            raise
        except Exception as exc:
            not_started = isinstance(exc, AiHttpCallNotStarted) or (
                isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ConnectionRefusedError))
            self._receive(exchange_id, outcome="not_started" if not_started else "unknown",
                error_code=type(exc).__name__, local_termination_confirmed=getattr(exc, "local_termination_confirmed", None))
            if not not_started and not isinstance(exc, AiProviderResultUnknown):
                raise AiProviderResultUnknown("provider_http_transport_result_unproven") from exc
            raise
        self._receive(exchange_id, outcome="response_received", response_hash=hashlib.sha256(body).hexdigest())
        return body

    def _receive(self, exchange_id, **facts):
        receive_exchange(self.session_factory, exchange_id, **facts)


def scoped_provider_gateway(source, session, *, config, provider_id, credentials, purpose, request_id):
    if (config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return source
    from app.ai_gateway import read_http

    scope = ExchangeScope(_scope_bindings(config), provider_id, credentials.model_name, purpose, request_id)
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    transport = TrackedProviderHttp(factory, scope, read_http, str(uuid4()))
    gateway = copy(source)
    gateway._http_transport = transport
    gateway._provider_http_chain_id = transport.chain_id
    return gateway


def _scope_bindings(config) -> tuple[dict, ...]:
    snapshots = dict(config.get("_ai_execution_timing") or {}).get("bindings") or ()
    slots = config.get("generation_slots") or ()
    selected_slots = config.get("_provider_http_slot_ids")
    if selected_slots is not None:
        slots = [slot for slot in slots if slot["slot_id"] in selected_slots]
        if len(slots) != len(set(selected_slots)):
            raise ValueError("provider_http_slot_scope_missing")
    if slots:
        job_ids = {slot["generation_job_id"] for slot in slots}
        snapshots = [item for item in snapshots if item["generation_job_id"] in job_ids]
        if len(snapshots) != len(job_ids):
            raise ValueError("provider_http_job_binding_missing")
    return tuple(deepcopy(snapshots))
