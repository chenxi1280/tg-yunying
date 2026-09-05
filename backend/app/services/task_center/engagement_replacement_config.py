"""Build fresh validated task input and preserve the original content authorization."""
from dataclasses import replace

from sqlalchemy import select

from app.common.state_hash import canonical_state_hash
from app.models import AccountPool, AccountStatus, AdultSubjectAttestation, TgAccount
from app.schemas import ChannelCommentTaskCreate, ChannelLikeTaskCreate, ChannelViewTaskCreate, GroupAIChatTaskCreate

from .ai_content_policy import AttestationSpec, create_adult_attestation
from .config_normalization import validated_type_config
from .task_ai_content_activation import validate_task_ai_content_config


CREATE_MODELS = {"group_ai_chat": GroupAIChatTaskCreate, "channel_comment": ChannelCommentTaskCreate,
    "channel_like": ChannelLikeTaskCreate, "channel_view": ChannelViewTaskCreate}
COMMON_FIELDS = ("name", "priority", "timezone", "scheduled_end", "max_duration_hours",
    "account_config", "pacing_config", "failure_policy")
DERIVED_PACING_FIELDS = frozenset({"fulfillment_soft_pacing_version", "daily_message_target"})
RUNTIME_PACING_FIELDS = frozenset({"rolling_window_days", "multi_day_rampup"})


def require_preserved_account_scope(session, task, payload):
    previous = task.account_config or {}
    mode = previous.get("selection_mode", "all")
    if mode == "all":
        expected = set(session.scalars(select(AccountPool.id).where(AccountPool.tenant_id == task.tenant_id,
            AccountPool.pool_purpose == "normal")))
        unassigned = session.scalar(select(TgAccount.id).where(TgAccount.tenant_id == task.tenant_id,
            TgAccount.account_identity == "normal", TgAccount.deleted_at.is_(None),
            TgAccount.status != AccountStatus.DISABLED.value, TgAccount.pool_id.is_(None)).limit(1))
        if unassigned is not None:
            raise ValueError("engagement_replacement_unassigned_legacy_member")
    elif mode == "group":
        expected = set(previous.get("account_group_ids") or [previous.get("account_group_id")])
    else:
        raise ValueError("engagement_replacement_legacy_account_scope_unsupported")
    if set(payload.account_group_ids) != expected:
        raise ValueError("engagement_replacement_account_scope_changed")


def replacement_payload(task, overrides):
    if task.type not in CREATE_MODELS:
        raise ValueError("engagement_replacement_task_type_invalid")
    config = validated_type_config(task.type, {**dict(task.type_config or {}), **overrides})
    if config.get("engagement_contract_version") != "unified_engagement_v1":
        raise ValueError("engagement_replacement_requires_unified_contract")
    model = CREATE_MODELS[task.type]
    values = {key: value for key, value in config.items() if key in model.model_fields}
    values.update(_replacement_common_values(task))
    return model(**values)


def _replacement_common_values(task):
    values = {key: getattr(task, key) for key in COMMON_FIELDS}
    # The formal creation normalizer supplies this server-owned contract field.
    values["pacing_config"] = {key: value for key, value in dict(task.pacing_config or {}).items()
        if key not in DERIVED_PACING_FIELDS | RUNTIME_PACING_FIELDS}
    if task.type == "group_ai_chat":
        values["group_ai_prejoin_channel_ids"] = list(task.group_ai_prejoin_channel_ids or [])
    return values


def preserve_runtime_pacing(old, new):
    source = {key: value for key, value in dict(old.pacing_config or {}).items() if key in RUNTIME_PACING_FIELDS}
    if "multi_day_rampup" in source and type(source["multi_day_rampup"]) is not bool:
        raise ValueError("engagement_replacement_runtime_rampup_invalid")
    if "rolling_window_days" in source and (type(source["rolling_window_days"]) is not int
            or source["rolling_window_days"] <= 0):
        raise ValueError("engagement_replacement_runtime_window_invalid")
    new.pacing_config = {**dict(new.pacing_config or {}), **source}


def authorization_snapshot(session, task):
    if task.type not in {"group_ai_chat", "channel_comment"}:
        return []
    config = dict(task.type_config or {})
    if config.get("ai_content_route_v2_enabled"):
        validate_task_ai_content_config(session, task)
    ids = tuple(config.get("ai_content_attestation_ids") or ())
    items = list(session.scalars(select(AdultSubjectAttestation).where(
        AdultSubjectAttestation.id.in_(ids)).order_by(AdultSubjectAttestation.id)))
    if len(items) != len(set(ids)):
        raise ValueError("engagement_replacement_authorization_missing")
    return [{"id": item.id, "hash": canonical_state_hash({column.name: getattr(item, column.name)
        for column in AdultSubjectAttestation.__table__.columns})} for item in items]


def replacement_authorizations(session, task, payload):
    ids = tuple(getattr(payload, "ai_content_attestation_ids", ()) or ())
    if not ids:
        return payload, {}
    if set(ids) != set((task.type_config or {}).get("ai_content_attestation_ids") or ()):
        raise ValueError("engagement_replacement_authorization_scope_changed")
    items = _lock_authorizations(session, task, ids)
    mapping = {}
    for old in items:
        mapping[old.id] = _clone_authorization(session, old).id
    if len(mapping) != len(set(ids)):
        raise ValueError("engagement_replacement_authorization_missing")
    return payload.model_copy(update={"ai_content_attestation_ids": [mapping[key] for key in ids]}), mapping


def _lock_authorizations(session, task, ids):
    items = list(session.scalars(select(AdultSubjectAttestation).where(
        AdultSubjectAttestation.id.in_(ids)).order_by(AdultSubjectAttestation.id)
        .with_for_update(read=True, nowait=True).execution_options(populate_existing=True)))
    if any(old.status != "active" or old.tenant_id != task.tenant_id
            or old.task_config_revision != task.config_revision for old in items):
        raise ValueError("engagement_replacement_authorization_changed")
    return items


def _clone_authorization(session, old):
    spec = AttestationSpec(tenant_id=old.tenant_id, scope_type=old.scope_type, scope_id=old.scope_id,
        subject_class=old.subject_class, evidence_codes=tuple(old.evidence_codes or ()),
        actor_user_id=old.actor_user_id, permission_snapshot=dict(old.permission_snapshot or {}),
        expires_at=old.expires_at, task_config_revision=old.task_config_revision,
        policy_version=old.policy_version)
    new = create_adult_attestation(session, replace(spec, task_config_revision=1))
    new.attested_at = old.attested_at
    return new
