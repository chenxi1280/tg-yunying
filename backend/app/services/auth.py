from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, hash_password, normalize_phone
from app.models import (
    AccountPool,
    AccountStatus,
    ActivationCode,
    AppUser,
    GroupAuthStatus,
    Material,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
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
    first_assignable_developer_app,
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

    tenant = Tenant(name="星火代运营", plan_name="试运行", account_quota=50, task_quota=5000)
    session.add(tenant)
    session.flush()
    default_pool = AccountPool(tenant_id=tenant.id, name="默认账号池", description="系统默认账号分组", is_default=True)
    session.add(default_pool)
    session.flush()
    seed_developer_apps(session)
    default_app = first_assignable_developer_app(session)

    accounts = [
        TgAccount(tenant_id=tenant.id, pool_id=default_pool.id, display_name="运营号 A", username="spark_ops_a", phone_masked="+86 138****1024", status=AccountStatus.ACTIVE.value, health_score=96, session_ciphertext="encrypted-demo", developer_app_id=default_app.id if default_app else None, developer_app_version=default_app.credentials_version if default_app else 1),
        TgAccount(tenant_id=tenant.id, pool_id=default_pool.id, display_name="运营号 B", username="spark_ops_b", phone_masked="+86 139****2048", status=AccountStatus.ACTIVE.value, health_score=88, session_ciphertext="encrypted-demo", developer_app_id=default_app.id if default_app else None, developer_app_version=default_app.credentials_version if default_app else 1),
        TgAccount(tenant_id=tenant.id, pool_id=default_pool.id, display_name="备用号 C", username="spark_ops_c", phone_masked="+852 ****7788", status=AccountStatus.WAITING_CODE.value, health_score=72, developer_app_id=default_app.id if default_app else None, developer_app_version=default_app.credentials_version if default_app else 1),
    ]
    session.add_all(accounts)
    session.flush()

    groups = [
        TgGroup(tenant_id=tenant.id, tg_peer_id="-100001", title="星火项目交流群", member_count=2480, auth_status=GroupAuthStatus.AUTHORIZED.value, topic_direction="活动答疑、产品体验、日常聊天"),
        TgGroup(tenant_id=tenant.id, tg_peer_id="-100002", title="新品内测社群", member_count=836, auth_status=GroupAuthStatus.READONLY.value, topic_direction="内测反馈、问题收集"),
        TgGroup(tenant_id=tenant.id, tg_peer_id="-100003", title="海外用户增长群", member_count=1289, auth_status=GroupAuthStatus.UNVERIFIED.value, topic_direction="增长案例、运营方法"),
    ]
    session.add_all(groups)
    session.flush()

    for group in groups:
        for account in accounts[:2]:
            session.add(TgGroupAccount(tenant_id=tenant.id, group_id=group.id, account_id=account.id, can_send=group.auth_status == GroupAuthStatus.AUTHORIZED.value))

    session.add_all(
        [
            Material(tenant_id=tenant.id, title="欢迎语模板", material_type="AI话术模板", content="欢迎新朋友，可以先看置顶公告，有问题直接问。", tags="欢迎,FAQ"),
            Material(tenant_id=tenant.id, title="活动提醒", material_type="文本", content="今晚 8 点有一轮答疑，感兴趣的朋友可以提前把问题发出来。", tags="活动,提醒"),
            Material(tenant_id=tenant.id, title="活动表情包", material_type="表情包", content="https://example.local/stickers/welcome.webp", tags="表情包,欢迎"),
            Material(tenant_id=tenant.id, title="产品海报", material_type="图片", content="https://example.local/images/product-poster.png", tags="图片,产品"),
        ]
    )
    audit(session, tenant_id=tenant.id, actor="system", action="初始化演示数据", target_type="tenant", target_id=str(tenant.id))
    seed_users(session, tenant.id)
    seed_ai_configuration(session)
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
        if changed:
            session.commit()
        return

    active_tenant_id = tenant_id or session.scalar(select(Tenant.id).order_by(Tenant.id))
    active_start = _now()
    active_end = active_start + timedelta(days=365)
    session.add_all(
        [
            AppUser(
                tenant_id=active_tenant_id,
                name="系统管理员",
                role="系统管理员",
                email="admin@demo.local",
                password_hash=hash_password("admin123"),
                subscription_status="active",
                subscription_started_at=active_start,
                subscription_expires_at=active_end,
            ),
            AppUser(
                tenant_id=active_tenant_id,
                name="演示普通用户",
                role="普通用户",
                email="ops@demo.local",
                password_hash=hash_password("ops123"),
                subscription_status="active",
                subscription_started_at=active_start,
                subscription_expires_at=active_end,
                last_activated_at=active_start,
            ),
        ]
    )
    session.commit()


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
    created: list[ActivationCode] = []
    for _ in range(payload.quantity):
        while True:
            code = f"{payload.plan_type[:1].upper()}{uuid4().hex[:15].upper()}"
            if not session.scalar(select(ActivationCode.id).where(ActivationCode.code == code)):
                break
        item = ActivationCode(
            code=code,
            plan_type=payload.plan_type,
            duration_days=duration_days,
            status="unused",
            created_by=actor,
            note=payload.note,
        )
        session.add(item)
        created.append(item)
    session.flush()
    audit(session, tenant_id=None, actor=actor, action="生成卡密", target_type="activation_code", target_id=str(created[0].id if created else "0"), detail=f"count={len(created)}; plan={payload.plan_type}")
    session.commit()
    for item in created:
        session.refresh(item)
    return created


def list_activation_codes(session: Session) -> list[ActivationCode]:
    return list(session.scalars(select(ActivationCode).order_by(ActivationCode.id.desc())))


def redeem_activation_code(session: Session, current_user: CurrentUser, payload: SubscriptionRedeemRequest) -> dict:
    user = session.get(AppUser, current_user.id)
    if not user:
        raise ValueError("user not found")
    code = session.scalar(select(ActivationCode).where(ActivationCode.code == payload.code.strip().upper()))
    if not code:
        raise ValueError("activation code not found")
    if code.status != "unused":
        raise ValueError("activation code already used")
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
    "create_user_registration",
    "ensure_seed_data",
    "list_activation_codes",
    "redeem_activation_code",
    "seed_users",
]
