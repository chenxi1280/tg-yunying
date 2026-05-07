from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser, hash_password, normalize_phone, verify_password
from app.config import get_settings
from app.models import (
    AccountPool,
    ActivationCode,
    AppUser,
    Tenant,
)
from app.schemas import (
    ActivationCodeCreateRequest,
    AuthRegisterRequest,
    SubscriptionRedeemRequest,
    TenantCreate,
)
from ._common import (
    _now,
    activation_plan_days,
    audit,
    subscription_days_remaining,
    unique_tenant_name,
)
from .account_pools import ensure_default_account_pool, seed_account_pools
from .ai_config import seed_ai_configuration
from .developer_apps import (
    backfill_account_developer_apps,
    seed_developer_apps,
)


def ensure_seed_data(session: Session) -> None:
    if session.scalar(select(func.count(Tenant.id))) > 0:
        seed_users(session)
        seed_developer_apps(session)
        seed_ai_configuration(session)
        seed_account_pools(session)
        backfill_account_developer_apps(session)
        session.commit()
        return

    tenant = Tenant(name="默认租户", plan_name="试运行", account_quota=50, task_quota=5000)
    session.add(tenant)
    session.flush()
    default_pool = AccountPool(tenant_id=tenant.id, name="默认账号池", description="系统默认账号分组", is_default=True)
    session.add(default_pool)
    session.flush()
    seed_developer_apps(session)
    seed_users(session, tenant.id)
    seed_ai_configuration(session)
    audit(session, tenant_id=tenant.id, actor="system", action="初始化本地工作区", target_type="tenant", target_id=str(tenant.id))
    session.commit()


def seed_users(session: Session, tenant_id: int | None = None) -> None:
    existing = session.scalar(select(func.count(AppUser.id))) or 0
    if existing:
        users = list(session.scalars(select(AppUser)))
        changed = False
        for user in users:
            if user.role == "平台管理员":
                user.role = "系统管理员"
                changed = True
            elif user.role != "系统管理员":
                user.role = "普通用户"
                changed = True
            if user.role == "系统管理员":
                user.subscription_status = "active"
            elif not user.subscription_status:
                user.subscription_status = "pending_activation"
                changed = True
            user.phone = normalize_phone(user.phone)
            if user.password_hash == "":
                user.password_hash = hash_password("admin123" if user.role == "系统管理员" else "ops123")
                changed = True
        if not any(user.role == "系统管理员" for user in users):
            session.add(_build_bootstrap_admin(session, tenant_id))
            changed = True
        else:
            bootstrap_admin = _get_bootstrap_admin(session)
            if not bootstrap_admin:
                session.add(_build_bootstrap_admin(session, tenant_id))
                changed = True
            elif bootstrap_admin.last_login_at is None and not verify_password(get_settings().admin_bootstrap_password, bootstrap_admin.password_hash):
                bootstrap_admin.password_hash = hash_password(get_settings().admin_bootstrap_password)
                changed = True
        if changed:
            session.commit()
        return

    session.add(_build_bootstrap_admin(session, tenant_id))
    _seed_default_operator(session, tenant_id)
    session.commit()


def _build_bootstrap_admin(session: Session, tenant_id: int | None = None) -> AppUser:
    active_tenant_id = tenant_id or session.scalar(select(Tenant.id).order_by(Tenant.id))
    settings = get_settings()
    admin_identifier, admin_email = _bootstrap_admin_identity()
    active_start = _now()
    active_end = active_start + timedelta(days=365)
    return AppUser(
        tenant_id=active_tenant_id,
        name=admin_identifier,
        role="系统管理员",
        email=admin_email,
        password_hash=hash_password(settings.admin_bootstrap_password),
        subscription_status="active",
        subscription_started_at=active_start,
        subscription_expires_at=active_end,
    )


def _bootstrap_admin_identity() -> tuple[str, str]:
    settings = get_settings()
    admin_identifier = settings.admin_bootstrap_username
    admin_email = (
        settings.admin_bootstrap_email.strip().lower()
        if settings.admin_bootstrap_email
        else admin_identifier.lower()
        if "@" in admin_identifier
        else f"{admin_identifier.lower()}@bootstrap.local"
    )
    return admin_identifier, admin_email


def _get_bootstrap_admin(session: Session) -> AppUser | None:
    admin_identifier, admin_email = _bootstrap_admin_identity()
    return session.scalar(
        select(AppUser).where(
            AppUser.role == "系统管理员",
            (AppUser.name == admin_identifier) | (AppUser.email == admin_email),
        )
    )


def _bootstrap_admin_exists(session: Session) -> bool:
    return bool(_get_bootstrap_admin(session))


def _seed_default_operator(session: Session, tenant_id: int | None = None) -> None:
    active_tenant_id = tenant_id or session.scalar(select(Tenant.id).order_by(Tenant.id))
    active_start = _now()
    active_end = active_start + timedelta(days=365)
    session.add_all(
        [
            AppUser(
                tenant_id=active_tenant_id,
                name="普通用户",
                role="普通用户",
                email="ops@bootstrap.local",
                password_hash=hash_password("ops123"),
                subscription_status="active",
                subscription_started_at=active_start,
                subscription_expires_at=active_end,
                last_activated_at=active_start,
            ),
        ]
    )


def change_user_password(session: Session, current_user: CurrentUser, current_password: str, new_password: str) -> AppUser:
    user = session.get(AppUser, current_user.id)
    if not user or not user.is_active:
        raise ValueError("user not found")
    if not verify_password(current_password, user.password_hash):
        raise ValueError("current password is incorrect")
    user.password_hash = hash_password(new_password)
    audit(session, tenant_id=user.tenant_id, actor=user.name, action="修改登录密码", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return user


def create_user_registration(session: Session, payload: AuthRegisterRequest) -> AppUser:
    email = payload.email.strip().lower()
    phone = normalize_phone(payload.phone)
    if session.scalar(select(AppUser.id).where(AppUser.email == email)):
        raise ValueError("email already registered")
    if phone and session.scalar(select(AppUser.id).where(AppUser.phone == phone)):
        raise ValueError("phone already registered")

    tenant = Tenant(name=unique_tenant_name(session, payload.name), plan_name="普通用户", account_quota=50, task_quota=5000)
    session.add(tenant)
    session.flush()
    seed_ai_configuration(session)
    ensure_default_account_pool(session, tenant.id)

    user = AppUser(
        tenant_id=tenant.id,
        name=payload.name.strip(),
        role="普通用户",
        email=email,
        phone=phone,
        password_hash=hash_password(payload.password),
        subscription_status="pending_activation",
    )
    session.add(user)
    session.flush()
    audit(session, tenant_id=tenant.id, actor="system", action="普通用户注册", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return user


def create_user_activation_codes(session: Session, payload: ActivationCodeCreateRequest, actor: str) -> list[ActivationCode]:
    duration_days = activation_plan_days(payload.plan_type)
    serial_prefix = (payload.serial_prefix.strip() or payload.plan_type[:1]).upper()
    batch_no = (payload.batch_no.strip() or "DEFAULT").upper()
    created: list[ActivationCode] = []
    for _ in range(payload.quantity):
        while True:
            code = f"{serial_prefix}-{batch_no}-{uuid4().hex[:10].upper()}"
            if not session.scalar(select(ActivationCode.id).where(ActivationCode.code == code)):
                break
        item = ActivationCode(
            code=code,
            plan_type=payload.plan_type,
            duration_days=duration_days,
            status="unused",
            batch_no=batch_no,
            serial_prefix=serial_prefix,
            created_by=actor,
            note=payload.note,
        )
        session.add(item)
        created.append(item)
    session.flush()
    audit(session, tenant_id=None, actor=actor, action="生成卡密", target_type="activation_code", target_id=str(created[0].id if created else "0"), detail=f"count={len(created)}; plan={payload.plan_type}; batch={batch_no}")
    session.commit()
    for item in created:
        session.refresh(item)
    return created


def list_activation_codes(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
    plan_type: str | None = None,
    batch_no: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    filters = []
    if search:
        like = f"%{search.strip()}%"
        filters.append(
            or_(
                ActivationCode.code.ilike(like),
                ActivationCode.batch_no.ilike(like),
                ActivationCode.serial_prefix.ilike(like),
                ActivationCode.created_by.ilike(like),
                AppUser.name.ilike(like),
                AppUser.email.ilike(like),
            )
        )
    if status:
        filters.append(ActivationCode.status == status)
    if plan_type:
        filters.append(ActivationCode.plan_type == plan_type)
    if batch_no:
        filters.append(ActivationCode.batch_no == batch_no.strip().upper())
    if start_at:
        filters.append(ActivationCode.created_at >= start_at)
    if end_at:
        filters.append(ActivationCode.created_at <= end_at)

    base = select(ActivationCode).outerjoin(AppUser, ActivationCode.redeemed_by_user_id == AppUser.id).where(*filters)
    total = session.scalar(
        select(func.count(ActivationCode.id))
        .select_from(ActivationCode)
        .outerjoin(AppUser, ActivationCode.redeemed_by_user_id == AppUser.id)
        .where(*filters)
    ) or 0
    items = list(
        session.scalars(
            base.options(joinedload(ActivationCode.redeemed_by_user))
            .order_by(ActivationCode.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def disable_activation_code(session: Session, code_id: int, actor: str) -> ActivationCode:
    code = session.get(ActivationCode, code_id)
    if not code:
        raise ValueError("activation code not found")
    if code.status != "unused" or code.redeemed_by_user_id is not None:
        raise ValueError("only unused activation codes can be disabled")
    code.status = "disabled"
    audit(session, tenant_id=None, actor=actor, action="停用卡密", target_type="activation_code", target_id=str(code.id), detail=f"batch={code.batch_no}; plan={code.plan_type}")
    session.commit()
    session.refresh(code)
    return code


def redeem_activation_code(session: Session, current_user: CurrentUser, payload: SubscriptionRedeemRequest) -> dict:
    user = session.get(AppUser, current_user.id)
    if not user:
        raise ValueError("user not found")
    code = session.scalar(select(ActivationCode).where(ActivationCode.code == payload.code.strip().upper()))
    if not code:
        raise ValueError("activation code not found")
    if code.status != "unused":
        raise ValueError("activation code is not available")
    now = _now()
    start_at = user.subscription_expires_at if user.subscription_expires_at and user.subscription_expires_at > now else now
    end_at = start_at + timedelta(days=code.duration_days)

    user.subscription_status = "active"
    user.subscription_started_at = user.subscription_started_at or start_at
    user.subscription_expires_at = end_at
    user.last_activated_at = now

    code.status = "redeemed"
    code.redeemed_by_user_id = user.id
    code.redeemed_at = now
    code.subscription_start_at = start_at
    code.subscription_end_at = end_at

    audit(session, tenant_id=user.tenant_id, actor=user.name, action="兑换卡密", target_type="activation_code", target_id=str(code.id), detail=code.plan_type)
    session.commit()
    return {
        "subscription_status": user.subscription_status,
        "subscription_started_at": user.subscription_started_at,
        "subscription_expires_at": user.subscription_expires_at,
        "subscription_days_remaining": subscription_days_remaining(user),
        "activation_code": code.code,
        "plan_type": code.plan_type,
        "duration_days": code.duration_days,
        "redeemed_at": code.redeemed_at,
    }


def create_tenant(session: Session, payload: TenantCreate) -> Tenant:
    tenant = Tenant(**payload.model_dump())
    session.add(tenant)
    session.flush()
    seed_ai_configuration(session)
    audit(session, tenant_id=tenant.id, actor="系统管理员", action="创建客户", target_type="tenant", target_id=str(tenant.id))
    session.commit()
    session.refresh(tenant)
    return tenant


__all__ = [
    "create_tenant",
    "create_user_activation_codes",
    "disable_activation_code",
    "create_user_registration",
    "change_user_password",
    "ensure_seed_data",
    "list_activation_codes",
    "redeem_activation_code",
    "seed_users",
]
