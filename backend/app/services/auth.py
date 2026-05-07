from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser, format_menu_permissions, hash_password, normalize_phone, parse_menu_permissions, verify_password
from app.config import get_settings
from app.models import (
    AccountPool,
    ActivationCode,
    AppUser,
    SubscriptionPlan,
    Tenant,
    UserTokenLedger,
)
from app.schemas import (
    ActivationCodeCreateRequest,
    AdminUserUpdate,
    AuthRegisterRequest,
    SubscriptionPlanCreate,
    SubscriptionPlanUpdate,
    SubscriptionRedeemRequest,
    TokenAdjustmentRequest,
    TenantCreate,
)
from ._common import (
    _now,
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
        seed_subscription_plans(session)
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
    seed_subscription_plans(session)
    seed_ai_configuration(session)
    audit(session, tenant_id=tenant.id, actor="system", action="初始化本地工作区", target_type="tenant", target_id=str(tenant.id))
    session.commit()


def seed_subscription_plans(session: Session) -> None:
    defaults = [
        {"plan_type": "monthly", "name": "月卡", "duration_days": 30, "token_quota": 500_000, "note": "默认月卡套餐"},
        {"plan_type": "yearly", "name": "年卡", "duration_days": 365, "token_quota": 6_000_000, "note": "默认年卡套餐"},
    ]
    changed = False
    for item in defaults:
        plan = session.scalar(select(SubscriptionPlan).where(SubscriptionPlan.plan_type == item["plan_type"]))
        if plan:
            continue
        session.add(SubscriptionPlan(**item))
        changed = True
    if changed:
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
            if user.role != "系统管理员" and not user.menu_permissions:
                user.menu_permissions = format_menu_permissions(parse_menu_permissions(None, role=user.role))
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
                token_balance=500_000,
                token_quota_total=500_000,
                menu_permissions=format_menu_permissions(parse_menu_permissions(None, role="普通用户")),
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


def serialize_admin_user(session: Session, user: AppUser) -> dict:
    tenant_name = None
    if user.tenant_id:
        tenant = session.get(Tenant, user.tenant_id)
        tenant_name = tenant.name if tenant else None
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "tenant_name": tenant_name,
        "name": user.name,
        "role": user.role,
        "email": user.email,
        "phone": user.phone,
        "subscription_status": user.subscription_status,
        "subscription_started_at": user.subscription_started_at,
        "subscription_expires_at": user.subscription_expires_at,
        "subscription_days_remaining": subscription_days_remaining(user),
        "token_balance": user.token_balance,
        "token_quota_total": user.token_quota_total,
        "menu_permissions": parse_menu_permissions(user.menu_permissions, role=user.role),
        "is_active": user.is_active,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def list_admin_users(session: Session, *, search: str | None = None, role: str | None = None, tenant_id: int | None = None) -> list[dict]:
    stmt = select(AppUser).order_by(AppUser.id.desc())
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(AppUser.name.ilike(like), AppUser.email.ilike(like), AppUser.phone.ilike(like)))
    if role:
        stmt = stmt.where(AppUser.role == role)
    if tenant_id is not None:
        stmt = stmt.where(AppUser.tenant_id == tenant_id)
    return [serialize_admin_user(session, user) for user in session.scalars(stmt)]


def update_admin_user(session: Session, user_id: int, payload: AdminUserUpdate, actor: str) -> dict:
    user = session.get(AppUser, user_id)
    if not user:
        raise ValueError("user not found")
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"]:
        email = data["email"].strip().lower()
        existing = session.scalar(select(AppUser.id).where(AppUser.email == email, AppUser.id != user.id))
        if existing:
            raise ValueError("email already registered")
        user.email = email
    if "phone" in data:
        phone = normalize_phone(data["phone"])
        if phone and session.scalar(select(AppUser.id).where(AppUser.phone == phone, AppUser.id != user.id)):
            raise ValueError("phone already registered")
        user.phone = phone
    for field in ["name", "role", "subscription_status", "is_active"]:
        if field in data and data[field] is not None:
            setattr(user, field, data[field])
    if "menu_permissions" in data:
        user.menu_permissions = format_menu_permissions(data["menu_permissions"])
    if user.role == "系统管理员":
        user.subscription_status = "active"
    audit(session, tenant_id=user.tenant_id, actor=actor, action="更新用户", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return serialize_admin_user(session, user)


def reset_admin_user_password(session: Session, user_id: int, new_password: str, actor: str) -> dict:
    user = session.get(AppUser, user_id)
    if not user:
        raise ValueError("user not found")
    user.password_hash = hash_password(new_password)
    audit(session, tenant_id=user.tenant_id, actor=actor, action="重置用户密码", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return serialize_admin_user(session, user)


def create_user_token_ledger(
    session: Session,
    *,
    user: AppUser,
    change_type: str,
    delta_tokens: int,
    reason: str,
    actor: str,
    related_activation_code_id: int | None = None,
    related_ai_usage_ledger_id: int | None = None,
) -> UserTokenLedger:
    ledger = UserTokenLedger(
        tenant_id=user.tenant_id,
        user_id=user.id,
        change_type=change_type,
        delta_tokens=delta_tokens,
        balance_after=user.token_balance,
        related_activation_code_id=related_activation_code_id,
        related_ai_usage_ledger_id=related_ai_usage_ledger_id,
        reason=reason,
        actor=actor,
    )
    session.add(ledger)
    return ledger


def adjust_user_tokens(session: Session, user_id: int, payload: TokenAdjustmentRequest, actor: str) -> dict:
    user = session.get(AppUser, user_id)
    if not user:
        raise ValueError("user not found")
    if payload.delta_tokens == 0:
        raise ValueError("delta_tokens cannot be zero")
    next_balance = user.token_balance + payload.delta_tokens
    if next_balance < 0:
        raise ValueError("token balance cannot be negative")
    user.token_balance = next_balance
    if payload.delta_tokens > 0:
        user.token_quota_total += payload.delta_tokens
    create_user_token_ledger(
        session,
        user=user,
        change_type="admin_adjustment",
        delta_tokens=payload.delta_tokens,
        reason=payload.reason,
        actor=actor,
    )
    audit(session, tenant_id=user.tenant_id, actor=actor, action="调整用户Token", target_type="app_user", target_id=str(user.id), detail=f"delta={payload.delta_tokens}")
    session.commit()
    session.refresh(user)
    return serialize_admin_user(session, user)


def list_user_token_ledgers(session: Session, user_id: int, limit: int = 100) -> list[UserTokenLedger]:
    if not session.get(AppUser, user_id):
        raise ValueError("user not found")
    return list(
        session.scalars(
            select(UserTokenLedger)
            .where(UserTokenLedger.user_id == user_id)
            .order_by(UserTokenLedger.id.desc())
            .limit(limit)
        )
    )


def list_subscription_plans(session: Session, *, active_only: bool = False) -> list[SubscriptionPlan]:
    stmt = select(SubscriptionPlan).order_by(SubscriptionPlan.id.asc())
    if active_only:
        stmt = stmt.where(SubscriptionPlan.is_active.is_(True))
    return list(session.scalars(stmt))


def create_subscription_plan(session: Session, payload: SubscriptionPlanCreate, actor: str) -> SubscriptionPlan:
    plan_type = payload.plan_type.strip().lower()
    if session.scalar(select(SubscriptionPlan.id).where(SubscriptionPlan.plan_type == plan_type)):
        raise ValueError("subscription plan already exists")
    data = payload.model_dump()
    data["plan_type"] = plan_type
    plan = SubscriptionPlan(**data)
    session.add(plan)
    session.flush()
    audit(session, tenant_id=None, actor=actor, action="新增套餐", target_type="subscription_plan", target_id=str(plan.id), detail=plan.plan_type)
    session.commit()
    session.refresh(plan)
    return plan


def update_subscription_plan(session: Session, plan_id: int, payload: SubscriptionPlanUpdate, actor: str) -> SubscriptionPlan:
    plan = session.get(SubscriptionPlan, plan_id)
    if not plan:
        raise ValueError("subscription plan not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(plan, field, value)
    plan.updated_at = _now()
    audit(session, tenant_id=None, actor=actor, action="更新套餐", target_type="subscription_plan", target_id=str(plan.id), detail=plan.plan_type)
    session.commit()
    session.refresh(plan)
    return plan


def _resolve_subscription_plan(session: Session, payload: ActivationCodeCreateRequest) -> SubscriptionPlan:
    seed_subscription_plans(session)
    if payload.plan_id is not None:
        plan = session.get(SubscriptionPlan, payload.plan_id)
    else:
        plan = session.scalar(select(SubscriptionPlan).where(SubscriptionPlan.plan_type == payload.plan_type.strip().lower()))
    if not plan:
        raise ValueError("subscription plan not found")
    if not plan.is_active:
        raise ValueError("subscription plan is disabled")
    return plan


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
        menu_permissions=format_menu_permissions(parse_menu_permissions(None, role="普通用户")),
    )
    session.add(user)
    session.flush()
    audit(session, tenant_id=tenant.id, actor="system", action="普通用户注册", target_type="app_user", target_id=str(user.id))
    session.commit()
    session.refresh(user)
    return user


def create_user_activation_codes(session: Session, payload: ActivationCodeCreateRequest, actor: str) -> list[ActivationCode]:
    plan = _resolve_subscription_plan(session, payload)
    duration_days = plan.duration_days
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
            plan_id=plan.id,
            plan_type=plan.plan_type,
            plan_name=plan.name,
            duration_days=duration_days,
            token_quota=plan.token_quota,
            status="unused",
            batch_no=batch_no,
            serial_prefix=serial_prefix,
            created_by=actor,
            note=payload.note,
        )
        session.add(item)
        created.append(item)
    session.flush()
    audit(session, tenant_id=None, actor=actor, action="生成卡密", target_type="activation_code", target_id=str(created[0].id if created else "0"), detail=f"count={len(created)}; plan={plan.plan_type}; tokens={plan.token_quota}; batch={batch_no}")
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
    if code.token_quota > 0:
        user.token_balance += code.token_quota
        user.token_quota_total += code.token_quota

    code.status = "redeemed"
    code.redeemed_by_user_id = user.id
    code.redeemed_at = now
    code.subscription_start_at = start_at
    code.subscription_end_at = end_at
    if code.token_quota > 0:
        create_user_token_ledger(
            session,
            user=user,
            change_type="activation_grant",
            delta_tokens=code.token_quota,
            reason=f"{code.plan_name or code.plan_type} 卡密激活赠送",
            actor=user.name,
            related_activation_code_id=code.id,
        )

    audit(session, tenant_id=user.tenant_id, actor=user.name, action="兑换卡密", target_type="activation_code", target_id=str(code.id), detail=code.plan_type)
    session.commit()
    return {
        "subscription_status": user.subscription_status,
        "subscription_started_at": user.subscription_started_at,
        "subscription_expires_at": user.subscription_expires_at,
        "subscription_days_remaining": subscription_days_remaining(user),
        "activation_code": code.code,
        "plan_type": code.plan_type,
        "plan_name": code.plan_name,
        "duration_days": code.duration_days,
        "token_quota": code.token_quota,
        "token_balance": user.token_balance,
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
    "adjust_user_tokens",
    "create_tenant",
    "create_subscription_plan",
    "create_user_activation_codes",
    "create_user_token_ledger",
    "disable_activation_code",
    "create_user_registration",
    "change_user_password",
    "ensure_seed_data",
    "list_activation_codes",
    "list_admin_users",
    "list_subscription_plans",
    "list_user_token_ledgers",
    "redeem_activation_code",
    "reset_admin_user_password",
    "seed_subscription_plans",
    "seed_users",
    "serialize_admin_user",
    "update_admin_user",
    "update_subscription_plan",
]
