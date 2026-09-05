"""Validate original Docker exit events without inferring a Telegram outcome."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re

from app.common.state_hash import canonical_state_hash
from app.timezone import as_beijing_aware


EVIDENCE_SCHEMA = 1
EXECUTOR_CONTRACT = "docker_pid1_local_telethon_v1"
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
EVENT_TIME = re.compile(r'time="([^\"]+)"')
EXIT_STATUS = re.compile(
    r'exitStatus="\{(\d+) (\d{4}-\d{2}-\d{2}) '
    r'(\d{2}:\d{2}:\d{2}(?:\.\d+)?) ([+-]\d{4}) [^}]+\}"'
)


@dataclass(frozen=True)
class WorkerExitProof:
    container_id: str
    source_host: str
    exited_at: datetime
    evidence_hash: str


def validate_exit_proofs(evidence, *, observed_at):
    source_host, collected_at = _evidence_origin(evidence, observed_at)
    records = evidence.get("exits")
    if not isinstance(records, list) or not records:
        raise ValueError("worker_exit_positive_evidence_required")
    proofs = tuple(_proof(record, source_host, collected_at) for record in records)
    if len({proof.container_id[:12] for proof in proofs}) != len(proofs):
        raise ValueError("worker_exit_container_prefix_ambiguous")
    return {proof.container_id[:12]: proof for proof in proofs}


def _evidence_origin(evidence, observed_at):
    if evidence.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("worker_exit_evidence_schema_invalid")
    if evidence.get("executor_contract") != EXECUTOR_CONTRACT:
        raise ValueError("worker_exit_executor_contract_unproven")
    source_host = evidence.get("source_host")
    if not isinstance(source_host, str) or not source_host.strip():
        raise ValueError("worker_exit_source_host_required")
    collected_at = _aware_time(evidence["collected_at"])
    if collected_at > as_beijing_aware(observed_at):
        raise ValueError("worker_exit_collection_in_future")
    return source_host, collected_at


def proof_for_attempt(proofs, attempt):
    proof = proofs.get(str(attempt.worker_id).split(":")[0])
    if proof is None or attempt.worker_id != f"{proof.container_id[:12]}:1":
        raise ValueError("worker_exit_original_process_unproven")
    if attempt.gateway_call_started_at is None:
        raise ValueError("worker_exit_attempt_not_called")
    if as_beijing_aware(attempt.gateway_call_started_at) >= proof.exited_at:
        raise ValueError("worker_exit_precedes_original_call")
    return proof


def _proof(record, source_host, collected_at):
    container_id = record.get("container_id")
    if not isinstance(container_id, str) or CONTAINER_ID.fullmatch(container_id) is None:
        raise ValueError("worker_exit_container_id_invalid")
    delete_message, delete_at = _event(record["delete_event"], source_host, container_id)
    exit_message, observed_at = _event(record["exit_event"], source_host, container_id)
    if not all(token in delete_message for token in (
            'msg="ignoring event"', "namespace=moby", "topic=/tasks/delete", 'type="*events.TaskDelete"')):
        raise ValueError("worker_exit_task_delete_unproven")
    if 'msg="ShouldRestart failed, container will not be restarted"' not in exit_message:
        raise ValueError("worker_exit_process_status_unproven")
    status = EXIT_STATUS.search(exit_message)
    if status is None:
        raise ValueError("worker_exit_actual_time_unproven")
    exited_at = _aware_time(f"{status[2]}T{status[3]}{status[4]}")
    if not (exited_at <= delete_at <= collected_at and exited_at <= observed_at <= collected_at):
        raise ValueError("worker_exit_event_time_conflict")
    return WorkerExitProof(container_id=container_id, source_host=source_host, exited_at=exited_at,
        evidence_hash=canonical_state_hash({"record": record, "source_host": source_host}))


def _event(event, source_host, container_id):
    if event.get("source_host") != source_host or not event.get("source_ref"):
        raise ValueError("worker_exit_event_source_mismatch")
    message = event.get("message")
    if not isinstance(message, str):
        raise ValueError("worker_exit_event_message_required")
    if hashlib.sha256(message.encode()).hexdigest() != event.get("message_sha256"):
        raise ValueError("worker_exit_event_hash_mismatch")
    if re.findall(r"(?:^|\s)container=([0-9a-f]+)(?:\s|$)", message) != [container_id]:
        raise ValueError("worker_exit_event_container_mismatch")
    stamp = EVENT_TIME.search(message)
    if stamp is None:
        raise ValueError("worker_exit_event_timestamp_required")
    return message, _aware_time(stamp[1])


def _aware_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("worker_exit_event_timezone_required")
    return parsed.astimezone(timezone.utc)
