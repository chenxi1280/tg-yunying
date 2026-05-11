from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountStatus, ArchivedMember, ArchivedMessage, GroupArchive, GroupAuthStatus, OperationTarget, TgAccount, TgGroup, TgGroupAccount
from app.schemas import ArchiveCreate

from ._common import _now, audit, gateway
from .developer_apps import credentials_for_account


def _invite_candidates(members: list[ArchivedMember]) -> list[ArchivedMember]:
    return [member for member in members if "可邀请" in (member.tags or "") or member.activity_score >= 70]


def _pick_archive_account(session: Session, group: TgGroup) -> TgAccount:
    link = session.scalar(
        select(TgGroupAccount)
        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(True),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.deleted_at.is_(None),
        )
        .order_by(TgAccount.health_score.desc())
    )
    if link:
        account = session.get(TgAccount, link.account_id)
        if account:
            return account
    account = session.scalar(
        select(TgAccount)
        .where(TgAccount.tenant_id == group.tenant_id, TgAccount.status == AccountStatus.ACTIVE.value, TgAccount.deleted_at.is_(None))
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    if not account:
        raise ValueError("当前没有可用于归档的在线账号")
    return account


def _collect_archive(session: Session, archive: GroupArchive, actor: str) -> GroupArchive:
    group = session.get(TgGroup, archive.group_id)
    if not group:
        raise ValueError("group not found")
    account = _pick_archive_account(session, group)
    archive.collection_account_id = account.id
    archive.started_at = archive.started_at or _now()
    archive.status = "归档中"
    session.commit()
    credentials = credentials_for_account(session, account)
    snapshot = gateway.fetch_group_archive(account.id, group.tg_peer_id, account.session_ciphertext, credentials)
    session.query(ArchivedMessage).filter(ArchivedMessage.archive_id == archive.id).delete()
    session.query(ArchivedMember).filter(ArchivedMember.archive_id == archive.id).delete()
    for index, item in enumerate(snapshot.messages, start=1):
        session.add(
            ArchivedMessage(
                tenant_id=archive.tenant_id,
                archive_id=archive.id,
                sender_peer_id="",
                remote_message_id=f"archive-{archive.id}-msg-{index}",
                sender_name=item.sender_name,
                content=item.content,
                message_type=item.message_type,
                sent_at=item.sent_at or _now(),
            )
        )
    for index, item in enumerate(snapshot.members, start=1):
        session.add(
            ArchivedMember(
                tenant_id=archive.tenant_id,
                archive_id=archive.id,
                peer_id=item.username or f"archive-{archive.id}-member-{index}",
                display_name=item.display_name,
                username=item.username,
                activity_score=item.activity_score,
                tags=item.tags,
                last_seen_at=_now(),
            )
        )
    archive.message_count = len(snapshot.messages)
    archive.member_count = len(snapshot.members)
    archive.summary = snapshot.summary
    archive.new_group_plan = snapshot.new_group_plan
    archive.status = "已完成"
    archive.failure_detail = ""
    archive.finished_at = _now()
    archive.last_synced_at = archive.finished_at
    audit(session, tenant_id=archive.tenant_id, actor=actor, action="完成群归档", target_type="group_archive", target_id=str(archive.id), detail=archive.sync_mode)
    session.commit()
    session.refresh(archive)
    return archive


def create_archive(session: Session, payload: ArchiveCreate, actor: str = "普通用户") -> GroupArchive:
    group = _archive_group_for_payload(session, payload)
    if not group:
        raise ValueError("group not found")

    archive = GroupArchive(
        tenant_id=payload.tenant_id,
        group_id=group.id,
        title=payload.title,
        status="排队中" if get_settings().tg_gateway_mode == "telethon" else "归档中",
        sync_mode="async" if get_settings().tg_gateway_mode == "telethon" else "sync",
    )
    session.add(archive)
    session.flush()
    audit(session, tenant_id=payload.tenant_id, actor=actor, action="创建群归档", target_type="group_archive", target_id=str(archive.id))
    session.commit()
    session.refresh(archive)
    if archive.sync_mode == "sync":
        return _collect_archive(session, archive, "tg-worker")
    return archive


def _archive_group_for_payload(session: Session, payload: ArchiveCreate) -> TgGroup | None:
    if payload.operation_target_id:
        target = session.get(OperationTarget, payload.operation_target_id)
        if not target or target.tenant_id != payload.tenant_id or target.target_type != "group":
            raise ValueError("运营目标不存在")
        if target.auth_status not in {GroupAuthStatus.AUTHORIZED.value, GroupAuthStatus.READONLY.value}:
            raise ValueError("运营目标未授权归档")
        return session.scalar(
            select(TgGroup).where(
                TgGroup.tenant_id == target.tenant_id,
                TgGroup.tg_peer_id == target.tg_peer_id,
            )
        )
    if payload.group_id:
        return session.get(TgGroup, payload.group_id)
    raise ValueError("请选择归档运营目标")


def get_archive_detail(session: Session, archive_id: int, message_search: str | None = None, member_search: str | None = None) -> dict:
    archive = session.get(GroupArchive, archive_id)
    if not archive:
        raise ValueError("archive not found")
    message_stmt = select(ArchivedMessage).where(ArchivedMessage.archive_id == archive.id, ArchivedMessage.tenant_id == archive.tenant_id)
    if message_search:
        like = f"%{message_search.strip()}%"
        message_stmt = message_stmt.where((ArchivedMessage.content.ilike(like)) | (ArchivedMessage.sender_name.ilike(like)))
    messages = list(session.scalars(message_stmt.order_by(ArchivedMessage.id)))
    member_stmt = select(ArchivedMember).where(ArchivedMember.archive_id == archive.id, ArchivedMember.tenant_id == archive.tenant_id)
    if member_search:
        like = f"%{member_search.strip()}%"
        member_stmt = member_stmt.where(
            (ArchivedMember.display_name.ilike(like))
            | (ArchivedMember.username.ilike(like))
            | (ArchivedMember.tags.ilike(like))
        )
    members = list(session.scalars(member_stmt.order_by(ArchivedMember.activity_score.desc())))
    return {"archive": archive, "messages": messages, "members": members, "invite_candidates": _invite_candidates(members)}


def export_archive(session: Session, archive_id: int, actor: str, export_format: str = "json") -> dict:
    detail = get_archive_detail(session, archive_id)
    archive: GroupArchive = detail["archive"]
    audit(session, tenant_id=archive.tenant_id, actor=actor, action="导出群归档", target_type="group_archive", target_id=str(archive.id), detail=export_format)
    session.commit()
    return {
        **detail,
        "export_format": export_format,
        "generated_at": _now(),
        "message_count": len(detail["messages"]),
        "member_count": len(detail["members"]),
    }

def process_archive(session: Session, archive_id: int) -> GroupArchive:
    archive = session.get(GroupArchive, archive_id)
    if not archive:
        raise ValueError("archive not found")
    if archive.status == "已完成":
        return archive
    archive.status = "归档中"
    archive.failure_detail = ""
    session.commit()
    try:
        return _collect_archive(session, archive, "tg-worker")
    except Exception as exc:  # noqa: BLE001
        archive = session.get(GroupArchive, archive_id)
        archive.status = "失败"
        archive.failure_detail = str(exc)
        archive.finished_at = _now()
        audit(session, tenant_id=archive.tenant_id, actor="tg-worker", action="群归档失败", target_type="group_archive", target_id=str(archive.id), detail=archive.failure_detail)
        session.commit()
        session.refresh(archive)
        return archive


def drain_archives(session_factory, limit: int = 10) -> int:
    count = 0
    with session_factory() as session:
        archive_ids = list(
            session.scalars(
                select(GroupArchive.id)
                .where(GroupArchive.status == "排队中", GroupArchive.sync_mode == "async")
                .order_by(GroupArchive.id.asc())
                .limit(limit)
            )
        )
    for archive_id in archive_ids:
        with session_factory() as session:
            process_archive(session, archive_id)
            count += 1
    return count


def rerun_archive(session: Session, archive_id: int, actor: str) -> GroupArchive:
    archive = session.get(GroupArchive, archive_id)
    if not archive:
        raise ValueError("archive not found")
    archive.status = "归档中" if get_settings().tg_gateway_mode != "telethon" else "排队中"
    archive.failure_detail = ""
    archive.started_at = None
    archive.finished_at = None
    audit(session, tenant_id=archive.tenant_id, actor=actor, action="重新执行群归档", target_type="group_archive", target_id=str(archive.id))
    session.commit()
    session.refresh(archive)
    if archive.sync_mode == "sync":
        return _collect_archive(session, archive, "tg-worker")
    return archive


__all__ = ["create_archive", "drain_archives", "export_archive", "get_archive_detail", "process_archive", "rerun_archive"]
