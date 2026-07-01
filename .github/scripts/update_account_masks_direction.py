from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, AiAccountVoiceProfile, AuditLog, TgAccount
from app.services._common import _now
from app.services.task_center.account_voice_profile_cache import refresh_voice_profile_cache_many


TENANT_ID = int(os.getenv("ACCOUNT_MASK_DIRECTION_TENANT_ID", "1"))
APPLY = os.getenv("ACCOUNT_MASK_DIRECTION_APPLY", "true").lower() == "true"
ACTOR = "github-actions-account-mask-direction-update"
SOURCE = "manual_direction_update"
REASON = "生产账号面具统一调整为伪装嫖客、男性、色情方向"

MASK_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "mask_name": "伪装嫖客",
        "audience_archetype": "男性成人场景浏览客",
        "identity_frame": "男性，装作来找色情服务的普通客人，先看反馈再问细节",
        "preference_tags": ["男性", "色情", "反馈", "避坑"],
        "age_band": "青年",
        "persona_experiences": ["常刷成人话题群", "熟悉本地场子黑话", "怕被骗或跑空"],
        "consumption_experiences": ["先问价格和位置", "会看别人体验反馈", "偏向低调试探"],
        "sentence_length": "短句",
        "interaction_habits": ["先接别人一句", "追问价格位置", "索要真实反馈"],
        "tone_strength": "试探",
        "lexical_preferences": ["别跑空", "有反馈没", "位置方便吗"],
        "emoji_policy": "少用",
        "forbidden_expressions": ["确实不错", "感觉挺靠谱", "这个不错"],
        "short_prompt_summary": "男性短句伪装嫖客先问价格位置和真实反馈",
    },
    {
        "mask_name": "男性色客",
        "audience_archetype": "偏直接的男性色情需求客",
        "identity_frame": "男性成人内容关注者，语气直接但不抢话，重点问服务和真实性",
        "preference_tags": ["男性", "色情", "服务", "真实"],
        "age_band": "中年",
        "persona_experiences": ["看过多个成人群", "熟悉套路和假照", "会对比口碑"],
        "consumption_experiences": ["先问服务边界", "看照片和评价", "确认不跑空再行动"],
        "sentence_length": "中句",
        "interaction_habits": ["补一句经历", "轻微质疑真假", "追问服务细节"],
        "tone_strength": "谨慎",
        "lexical_preferences": ["真实吗", "别套路", "能具体说下吗"],
        "emoji_policy": "不用表情",
        "forbidden_expressions": ["稳了", "包好", "绝对靠谱"],
        "short_prompt_summary": "男性中句色情需求客谨慎追问服务真假和反馈",
    },
    {
        "mask_name": "寻欢男客",
        "audience_archetype": "本地寻欢男性客",
        "identity_frame": "男性，本地寻欢口吻，关注位置距离、时间安排和避坑反馈",
        "preference_tags": ["男性", "色情", "本地", "位置"],
        "age_band": "青年",
        "persona_experiences": ["熟悉本地地名", "会问距离远近", "怕临时变卦"],
        "consumption_experiences": ["看完反馈再约", "偏向近距离场子", "先确认时间"],
        "sentence_length": "短句",
        "interaction_habits": ["接话问位置", "追问时间", "提醒别夸太满"],
        "tone_strength": "轻松",
        "lexical_preferences": ["在哪边", "今晚有吗", "别太远"],
        "emoji_policy": "少用",
        "forbidden_expressions": ["完美", "真香", "太顶了"],
        "short_prompt_summary": "本地男性短句寻欢客重点问位置时间和避坑",
    },
    {
        "mask_name": "成人观望客",
        "audience_archetype": "观望型男性成人内容客",
        "identity_frame": "男性，围绕色情话题保持观望，先听别人反馈再少量追问",
        "preference_tags": ["男性", "色情", "观望", "反馈"],
        "age_band": "中年",
        "persona_experiences": ["经常潜水看群聊", "不轻易下判断", "在意踩坑记录"],
        "consumption_experiences": ["等多人反馈", "先收藏信息", "偶尔问价"],
        "sentence_length": "中句",
        "interaction_habits": ["先附和一句", "问有没有踩坑", "把话题拉回反馈"],
        "tone_strength": "克制",
        "lexical_preferences": ["先看看", "有人去过没", "反馈多点再说"],
        "emoji_policy": "不用表情",
        "forbidden_expressions": ["冲就完了", "闭眼上", "绝对没问题"],
        "short_prompt_summary": "男性中句成人观望客先看多人反馈再追问",
    },
)


def main() -> int:
    with SessionLocal() as session:
        accounts = _target_accounts(session)
        current_by_id = _latest_profiles(session, [account.id for account in accounts])
        rows = _build_rows(accounts, current_by_id)
        payload = _result_payload(accounts, current_by_id, rows)
        if APPLY:
            _apply_rows(session, rows, current_by_id)
            session.commit()
            payload["verified_active_count"] = _verified_active_count(session, accounts)
        _assert_success(payload)
    print("ACCOUNT_MASK_DIRECTION_UPDATE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _target_accounts(session) -> list[TgAccount]:
    return list(
        session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
            )
            .order_by(TgAccount.id.asc())
        )
    )


def _latest_profiles(session, account_ids: list[int]) -> dict[int, AiAccountVoiceProfile]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(AiAccountVoiceProfile)
        .where(
            AiAccountVoiceProfile.tenant_id == TENANT_ID,
            AiAccountVoiceProfile.account_id.in_(account_ids),
        )
        .order_by(AiAccountVoiceProfile.account_id.asc(), AiAccountVoiceProfile.version.desc())
    )
    result: dict[int, AiAccountVoiceProfile] = {}
    for row in rows:
        result.setdefault(row.account_id, row)
    return result


def _build_rows(
    accounts: list[TgAccount],
    current_by_id: dict[int, AiAccountVoiceProfile],
) -> list[AiAccountVoiceProfile]:
    rows: list[AiAccountVoiceProfile] = []
    for index, account in enumerate(accounts):
        current = current_by_id.get(account.id)
        rows.append(_row_for_account(account.id, index, current))
    return rows


def _row_for_account(
    account_id: int,
    index: int,
    current: AiAccountVoiceProfile | None,
) -> AiAccountVoiceProfile:
    variant = MASK_VARIANTS[index % len(MASK_VARIANTS)]
    return AiAccountVoiceProfile(
        tenant_id=TENANT_ID,
        account_id=account_id,
        version=int(current.version if current else 0) + 1,
        source=SOURCE,
        status="active",
        quality_status="active",
        last_rebuilt_at=_now(),
        updated_by=ACTOR,
        **variant,
    )


def _apply_rows(
    session,
    rows: list[AiAccountVoiceProfile],
    current_by_id: dict[int, AiAccountVoiceProfile],
) -> None:
    for row in rows:
        current = current_by_id.get(row.account_id)
        if current and current.status == "active":
            current.status = "superseded"
        session.add(row)
        _audit(session, row)
    session.flush()
    refresh_voice_profile_cache_many(rows)


def _audit(session, row: AiAccountVoiceProfile) -> None:
    session.add(
        AuditLog(
            tenant_id=TENANT_ID,
            actor=ACTOR,
            action="批量更新账号面具方向",
            target_type="ai_account_voice_profile",
            target_id=str(row.account_id),
            detail=f"version={row.version}; source={SOURCE}; reason={REASON}",
        )
    )


def _verified_active_count(session, accounts: list[TgAccount]) -> int:
    ids = [account.id for account in accounts]
    if not ids:
        return 0
    rows = _latest_profiles(session, ids)
    return sum(1 for row in rows.values() if row.status == "active" and row.source == SOURCE)


def _result_payload(
    accounts: list[TgAccount],
    current_by_id: dict[int, AiAccountVoiceProfile],
    rows: list[AiAccountVoiceProfile],
) -> dict[str, Any]:
    return {
        "tenant_id": TENANT_ID,
        "apply": APPLY,
        "target_account_count": len(accounts),
        "existing_profile_count": len(current_by_id),
        "created_profile_count": len(rows) if APPLY else 0,
        "planned_profile_count": len(rows),
        "source": SOURCE,
        "reason": REASON,
        "mask_names": sorted({row.mask_name for row in rows}),
        "sample_rows": _sample_rows(rows),
    }


def _sample_rows(rows: list[AiAccountVoiceProfile]) -> list[dict[str, Any]]:
    return [
        {
            "account_id": row.account_id,
            "version": row.version,
            "mask_name": row.mask_name,
            "audience_archetype": row.audience_archetype,
            "identity_frame": row.identity_frame,
            "preference_tags": row.preference_tags,
            "short_prompt_summary": row.short_prompt_summary,
        }
        for row in rows[:8]
    ]


def _assert_success(payload: dict[str, Any]) -> None:
    target = int(payload.get("target_account_count") or 0)
    if target <= 0:
        raise RuntimeError("no active accounts found for account mask direction update")
    if not APPLY:
        return
    verified = int(payload.get("verified_active_count") or 0)
    if verified != target:
        raise RuntimeError(f"account mask update verification failed: {verified}/{target}")


if __name__ == "__main__":
    raise SystemExit(main())
