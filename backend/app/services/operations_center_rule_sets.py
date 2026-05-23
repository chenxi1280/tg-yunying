from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RuleSet, RuleSetVersion, Task
from app.schemas.operations_center import RuleSetBoundTaskOut, RuleSetCreate, RuleSetOut, RuleSetVersionCreate
from app.services._common import _now, audit
from app.services.operations_center_defaults import (
    DEFAULT_RELAY_FILTERS,
    DEFAULT_RELAY_OUTPUT_CHECKS,
    DEFAULT_RULE_SET_DESCRIPTION,
    DEFAULT_RULE_SET_NAME,
    DEFAULT_RULE_TASK_TYPES,
    LEGACY_DEFAULT_RELAY_RULE_SET_NAME,
    _default_relay_filters,
    _default_relay_output_checks,
)
from app.services.operations_center_utils import as_int as _as_int, iso as _iso

VERSION_DIFF_FIELDS = ("filters", "output_checks", "transforms", "routing", "account_strategy", "rate_limits", "retry_policy")


def list_rule_sets(session: Session, tenant_id: int) -> list[RuleSetOut]:
    _ensure_default_rule_set(session, tenant_id)
    rule_sets = list(session.scalars(select(RuleSet).where(RuleSet.tenant_id == tenant_id).order_by(RuleSet.id.asc())))
    versions = list(
        session.scalars(
            select(RuleSetVersion)
            .where(RuleSetVersion.tenant_id == tenant_id, RuleSetVersion.rule_set_id.in_([item.id for item in rule_sets]))
            .order_by(RuleSetVersion.rule_set_id.asc(), RuleSetVersion.version.desc())
        )
    )
    by_set: dict[int, list[RuleSetVersion]] = {}
    for version in versions:
        by_set.setdefault(version.rule_set_id, []).append(version)
    return [_rule_set_out(rule_set, by_set.get(rule_set.id, [])) for rule_set in rule_sets]


def _ensure_default_rule_set(session: Session, tenant_id: int) -> RuleSet:
    existing = session.scalar(
        select(RuleSet).where(
            RuleSet.tenant_id == tenant_id,
            RuleSet.name == DEFAULT_RULE_SET_NAME,
        )
    )
    if not existing:
        existing = session.scalar(
            select(RuleSet).where(
                RuleSet.tenant_id == tenant_id,
                RuleSet.name == LEGACY_DEFAULT_RELAY_RULE_SET_NAME,
            )
        )
    if existing:
        changed = False
        if existing.name == LEGACY_DEFAULT_RELAY_RULE_SET_NAME:
            existing.name = DEFAULT_RULE_SET_NAME
            changed = True
        if existing.description != DEFAULT_RULE_SET_DESCRIPTION:
            existing.description = DEFAULT_RULE_SET_DESCRIPTION
            changed = True
        if set(existing.task_types or []) != set(DEFAULT_RULE_TASK_TYPES):
            existing.task_types = DEFAULT_RULE_TASK_TYPES
            changed = True
        default_policy = dict(existing.default_policy or {})
        if default_policy.get("version_binding") != "follow_current":
            default_policy["version_binding"] = "follow_current"
            existing.default_policy = default_policy
            changed = True
        versions = list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == existing.id).order_by(RuleSetVersion.version.desc())))
        if not versions:
            version = RuleSetVersion(
                tenant_id=tenant_id,
                rule_set_id=existing.id,
                version=1,
                status="published",
                filters=_default_relay_filters(),
                output_checks=_default_relay_output_checks(),
                transforms={},
                routing={},
                account_strategy={},
                rate_limits={},
                retry_policy={},
                created_by="system",
                published_by="system",
                published_at=_now(),
            )
            session.add(version)
            session.flush()
            existing.active_version_id = version.id
            changed = True
        elif not existing.active_version_id:
            published = next((item for item in versions if item.status == "published"), versions[0])
            existing.active_version_id = published.id
            if published.status != "published":
                published.status = "published"
                published.published_by = published.published_by or "system"
                published.published_at = published.published_at or _now()
            changed = True
        if changed:
            existing.updated_at = _now()
            session.commit()
            session.refresh(existing)
        return existing
    rule_set = RuleSet(
        tenant_id=tenant_id,
        name=DEFAULT_RULE_SET_NAME,
        description=DEFAULT_RULE_SET_DESCRIPTION,
        status="active",
        task_types=DEFAULT_RULE_TASK_TYPES,
        default_policy={"input_failure": "skip", "output_failure": "transform_once_drop", "version_binding": "follow_current"},
    )
    session.add(rule_set)
    session.flush()
    version = RuleSetVersion(
        tenant_id=tenant_id,
        rule_set_id=rule_set.id,
        version=1,
        status="published",
        filters=_default_relay_filters(),
        output_checks=_default_relay_output_checks(),
        transforms={},
        routing={},
        account_strategy={},
        rate_limits={},
        retry_policy={},
        created_by="system",
        published_by="system",
        published_at=_now(),
    )
    session.add(version)
    session.flush()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    audit(
        session,
        tenant_id=tenant_id,
        actor="system",
        action="初始化默认运营规则集",
        target_type="rule_set",
        target_id=str(rule_set.id),
        detail=rule_set.name,
    )
    session.commit()
    session.refresh(rule_set)
    return rule_set


def create_rule_set(session: Session, tenant_id: int, payload: RuleSetCreate, actor: str) -> RuleSetOut:
    if session.scalar(select(RuleSet.id).where(RuleSet.tenant_id == tenant_id, RuleSet.name == payload.name).limit(1)):
        raise ValueError("同名规则集已存在")
    rule_set = RuleSet(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        status="active",
        task_types=payload.task_types,
        default_policy=payload.default_policy,
    )
    session.add(rule_set)
    session.flush()
    version = _new_rule_set_version(session, tenant_id, rule_set.id, 1, payload, actor)
    rule_set.active_version_id = version.id
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="创建规则集", target_type="rule_set", target_id=str(rule_set.id), detail=rule_set.name)
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, [version])


def create_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建规则集版本", target_type="rule_set", target_id=str(rule_set.id), detail=f"v{version.version}")
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def update_rule_set_config(session: Session, tenant_id: int, rule_set_id: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    current = session.get(RuleSetVersion, rule_set.active_version_id) if rule_set.active_version_id else None
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    diff_fields = _version_diff_fields(current, version) if current else list(VERSION_DIFF_FIELDS)
    for old_version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.status == "published")):
        old_version.status = "archived"
        old_version.updated_at = _now()
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    version.updated_at = _now()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    from_label = f"v{current.version}" if current else "-"
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="更新规则集配置并发布",
        target_type="rule_set",
        target_id=str(rule_set.id),
        detail=_version_action_detail(f"{from_label}->v{version.version}", payload.publish_reason or payload.version_note, diff_fields),
    )
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def copy_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str, reason: str = "") -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    source = _get_rule_set_version(session, tenant_id, rule_set.id, version_id)
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    payload = _version_payload_from_row(source, version_note=f"复制自 v{source.version}")
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="复制规则集版本为草稿",
        target_type="rule_set",
        target_id=str(rule_set.id),
        detail=_version_action_detail(f"v{source.version}->v{version.version}", reason, _version_diff_fields(source, version)),
    )
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def publish_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str, reason: str = "") -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    version = _get_rule_set_version(session, tenant_id, rule_set.id, version_id)
    current = session.get(RuleSetVersion, rule_set.active_version_id) if rule_set.active_version_id else None
    diff_fields = _version_diff_fields(current, version) if current else VERSION_DIFF_FIELDS
    for old_version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.status == "published")):
        old_version.status = "archived"
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    version.updated_at = _now()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    from_label = f"v{current.version}" if current else "-"
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="发布规则集版本",
        target_type="rule_set",
        target_id=str(rule_set.id),
        detail=_version_action_detail(f"{from_label}->v{version.version}", reason, diff_fields),
    )
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def rollback_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str, reason: str = "") -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    source = _get_rule_set_version(session, tenant_id, rule_set.id, version_id)
    current = session.get(RuleSetVersion, rule_set.active_version_id) if rule_set.active_version_id else None
    diff_fields = _version_diff_fields(current, source) if current else VERSION_DIFF_FIELDS
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    payload = _version_payload_from_row(source, version_note=f"回滚自 v{source.version}")
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    for old_version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.status == "published")):
        old_version.status = "archived"
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    version.updated_at = _now()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    from_label = f"v{current.version}" if current else "-"
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="回滚规则集版本",
        target_type="rule_set",
        target_id=str(rule_set.id),
        detail=_version_action_detail(f"{from_label}->v{version.version}; source=v{source.version}", reason, diff_fields),
    )
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def list_rule_set_bound_tasks(session: Session, tenant_id: int, rule_set_id: int) -> list[RuleSetBoundTaskOut]:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    version_ids = {
        version_id
        for version_id in session.scalars(select(RuleSetVersion.id).where(RuleSetVersion.tenant_id == tenant_id, RuleSetVersion.rule_set_id == rule_set.id))
    }
    tasks = list(session.scalars(select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None)).order_by(Task.updated_at.desc())))
    rows: list[RuleSetBoundTaskOut] = []
    for task in tasks:
        config = task.type_config or {}
        config_rule_set_id = _as_int(config.get("rule_set_id"))
        config_version_id = _as_int(config.get("rule_set_version_id"))
        if config_rule_set_id != rule_set.id and config_version_id not in version_ids:
            continue
        resolved = config_version_id or rule_set.active_version_id
        rows.append(
            RuleSetBoundTaskOut(
                id=task.id,
                name=task.name,
                type=task.type,
                status=task.status,
                binding_mode="fixed_version" if config_version_id else "follow_current",
                rule_set_id=config_rule_set_id or rule_set.id,
                rule_set_version_id=config_version_id,
                resolved_rule_set_version_id=resolved,
                created_at=_iso(task.created_at) or "",
                updated_at=_iso(task.updated_at) or "",
            )
        )
    return rows



def _get_rule_set(session: Session, tenant_id: int, rule_set_id: int) -> RuleSet:
    rule_set = session.get(RuleSet, rule_set_id)
    if not rule_set or rule_set.tenant_id != tenant_id:
        raise ValueError("规则集不存在")
    return rule_set


def _get_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int) -> RuleSetVersion:
    version = session.get(RuleSetVersion, version_id)
    if not version or version.tenant_id != tenant_id or version.rule_set_id != rule_set_id:
        raise ValueError("规则集版本不存在")
    return version


def _version_payload_from_row(version: RuleSetVersion, *, version_note: str) -> RuleSetVersionCreate:
    return RuleSetVersionCreate(
        version_note=version_note,
        filters=dict(version.filters or {}),
        output_checks=dict(version.output_checks or {}),
        transforms=dict(version.transforms or {}),
        routing=dict(version.routing or {}),
        account_strategy=dict(version.account_strategy or {}),
        rate_limits=dict(version.rate_limits or {}),
        retry_policy=dict(version.retry_policy or {}),
    )


def _version_diff_fields(before: RuleSetVersion | None, after: RuleSetVersion) -> list[str]:
    if before is None:
        return list(VERSION_DIFF_FIELDS)
    changed: list[str] = []
    for field in VERSION_DIFF_FIELDS:
        if (getattr(before, field) or {}) != (getattr(after, field) or {}):
            changed.append(field)
    return changed


def _version_action_detail(change: str, reason: str, diff_fields: list[str]) -> str:
    cleaned_reason = (reason or "").strip() or "未填写"
    diff = ",".join(diff_fields) if diff_fields else "none"
    return f"{change}; reason={cleaned_reason}; diff={diff}"


def _new_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetVersion:
    row = RuleSetVersion(
        tenant_id=tenant_id,
        rule_set_id=rule_set_id,
        version=version,
        status="draft",
        version_note=payload.version_note,
        filters=payload.filters,
        output_checks=payload.output_checks,
        transforms=payload.transforms,
        routing=payload.routing,
        account_strategy=payload.account_strategy,
        rate_limits=payload.rate_limits,
        retry_policy=payload.retry_policy,
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def _rule_set_out(rule_set: RuleSet, versions: list[RuleSetVersion]) -> RuleSetOut:
    return RuleSetOut(
        id=rule_set.id,
        tenant_id=rule_set.tenant_id,
        name=rule_set.name,
        description=rule_set.description,
        status=rule_set.status,
        task_types=rule_set.task_types or [],
        default_policy=rule_set.default_policy or {},
        active_version_id=rule_set.active_version_id,
        versions=[
            {
                "id": version.id,
                "tenant_id": version.tenant_id,
                "rule_set_id": version.rule_set_id,
                "version": version.version,
                "status": version.status,
                "version_note": version.version_note,
                "filters": version.filters or {},
                "output_checks": version.output_checks or {},
                "transforms": version.transforms or {},
                "routing": version.routing or {},
                "account_strategy": version.account_strategy or {},
                "rate_limits": version.rate_limits or {},
                "retry_policy": version.retry_policy or {},
                "created_by": version.created_by,
                "published_by": version.published_by,
                "published_at": _iso(version.published_at),
                "created_at": _iso(version.created_at) or "",
                "updated_at": _iso(version.updated_at) or "",
            }
            for version in versions
        ],
        created_at=_iso(rule_set.created_at) or "",
        updated_at=_iso(rule_set.updated_at) or "",
    )
