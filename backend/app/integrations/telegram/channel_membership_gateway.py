"""Separate accepted join requests from observed membership and unknown calls."""

from datetime import datetime, timezone

from telethon import functions
from telethon.errors import InviteRequestSentError, UserAlreadyParticipantError

from app.models import FailureType
from .contracts import ChannelMembershipResult
from .join_request_verification import capture_join_request_baseline, resolve_join_request


JOIN_REQUEST_PENDING = "join_request_pending"


async def ensure_membership(client, target, *, invite_hash, verify_join_request, map_error):
    try:
        baseline = None
        if invite_hash:
            request = functions.messages.ImportChatInviteRequest(invite_hash)
        else:
            reference = int(target) if target.lstrip("-").isdigit() else target.lstrip("@")
            entity = await client.get_entity(reference)
            if verify_join_request and getattr(entity, "megagroup", False) and getattr(entity, "join_request", False):
                baseline = await capture_join_request_baseline(client, entity)
            request = functions.channels.JoinChannelRequest(entity)
    except Exception as exc:
        return _failed_result(exc, map_error=map_error, mutation_started=False)
    return await _submit_join(client, request, baseline=baseline, map_error=map_error)


async def _submit_join(client, request, *, baseline, map_error):
    requested_at = datetime.now(timezone.utc)
    try:
        await client(request)
    except UserAlreadyParticipantError:
        return ChannelMembershipResult(True, detail="already_joined", membership_status="already_joined",
                                       remote_mutation_started=False)
    except InviteRequestSentError:
        return await _pending_result(client, baseline, requested_at=requested_at)
    except Exception as exc:
        return _failed_result(exc, map_error=map_error, mutation_started=None)
    return ChannelMembershipResult(True, detail="joined", membership_status="joined", remote_mutation_started=True)


async def _pending_result(client, baseline, *, requested_at):
    evidence = {"join_request_submitted": True, "requested_at": requested_at.isoformat(),
                "membership_observed": False, "verification_status": "private_verification_not_enabled_or_supported"}
    if baseline is not None:
        try:
            evidence = await resolve_join_request(client, baseline, requested_at=requested_at)
        except Exception as exc:
            evidence = {**evidence, "verification_status": "verification_observation_failed", "error_type": type(exc).__name__}
    if evidence.get("membership_observed"):
        return ChannelMembershipResult(True, detail="join_request_verified_membership_observed", membership_status="joined",
                                       remote_mutation_started=True, join_request_evidence=evidence)
    mutation = None if evidence.get("verification_status") in {"callback_result_unknown", "verification_observation_failed"} else True
    return ChannelMembershipResult(False, "待审批", JOIN_REQUEST_PENDING,
        "已提交入群申请，尚未观察到审批后的成员关系", "pending_approval",
        remote_mutation_started=mutation, join_request_evidence=evidence)


def _failed_result(exc, *, map_error, mutation_started):
    mapped = map_error(exc)
    return ChannelMembershipResult(False, "失败", mapped.failure_type or FailureType.PEER_INVALID.value,
                                   mapped.detail or str(exc), "failed", remote_mutation_started=mutation_started)
