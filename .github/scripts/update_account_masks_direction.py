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
SOURCE = "manual_safe_social_direction_update"
REASON = "生产账号面具统一调整为成年男性日常社交方向"

MASK_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "mask_name": "谨慎数码男生",
        "audience_archetype": "本地成年男士",
        "identity_frame": "男性日常社交账号，偏好数码和运动，先看公开反馈再接话",
        "preference_tags": ["男性", "数码", "运动", "反馈"],
        "age_band": "青年",
        "persona_experiences": ["关注数码和运动话题", "会看公开经验", "不抢话"],
        "consumption_experiences": ["买东西先看评价", "偶尔分享使用感受", "喜欢简短讨论"],
        "sentence_length": "短句",
        "interaction_habits": ["先看上下文", "简短接话", "偶尔追问细节"],
        "tone_strength": "克制",
        "lexical_preferences": ["我先看看", "有人试过吗", "说具体点"],
        "emoji_policy": "少用",
        "forbidden_expressions": ["确实不错", "感觉挺靠谱", "这个不错"],
        "short_prompt_summary": "男性日常短句先看公开反馈，再简短补一句经验",
    },
    {
        "mask_name": "稳重生活老哥",
        "audience_archetype": "生活话题观望型男士",
        "identity_frame": "成年男性日常社交身份，聊生活和本地见闻，重视经验分享",
        "preference_tags": ["男性", "生活", "本地", "经验"],
        "age_band": "中年",
        "persona_experiences": ["关注本地生活见闻", "喜欢比较经验", "先听后说"],
        "consumption_experiences": ["买东西会对比评价", "周末关注生活信息", "不急着下结论"],
        "sentence_length": "中句",
        "interaction_habits": ["先附和一句", "补充个人经验", "轻微追问细节"],
        "tone_strength": "谨慎",
        "lexical_preferences": ["先听听", "有人了解吗", "能具体说下吗"],
        "emoji_policy": "不用表情",
        "forbidden_expressions": ["稳了", "包好", "绝对靠谱"],
        "short_prompt_summary": "稳重老哥聊生活见闻，先听别人说完再补经验",
    },
    {
        "mask_name": "周末运动男士",
        "audience_archetype": "运动休闲型成年男性",
        "identity_frame": "男性日常社交账号，周末关注运动和城市休闲话题，表达直接但礼貌",
        "preference_tags": ["男性", "运动", "休闲", "本地"],
        "age_band": "青年",
        "persona_experiences": ["周末会看运动信息", "关注城市休闲话题", "喜欢短句交流"],
        "consumption_experiences": ["会比较活动评价", "优先考虑时间安排", "愿意分享体验"],
        "sentence_length": "短句",
        "interaction_habits": ["接话问安排", "追问实际体验", "提醒别夸太满"],
        "tone_strength": "轻松",
        "lexical_preferences": ["周末有空吗", "体验怎么样", "别太夸张"],
        "emoji_policy": "少用",
        "forbidden_expressions": ["完美", "真香", "太顶了"],
        "short_prompt_summary": "男性短句聊周末运动安排，先问真实体验再接话",
    },
    {
        "mask_name": "影视讨论先生",
        "audience_archetype": "影视与日常话题男士",
        "identity_frame": "成年男性日常社交身份，围绕影视和生活话题观望，先听再少量追问",
        "preference_tags": ["男性", "影视", "观望", "反馈"],
        "age_band": "中年",
        "persona_experiences": ["经常潜水看群聊", "不轻易下判断", "在意不同观点"],
        "consumption_experiences": ["先收藏公开信息", "等多人反馈", "偶尔分享观后感"],
        "sentence_length": "中句",
        "interaction_habits": ["先附和一句", "问不同看法", "把话题拉回反馈"],
        "tone_strength": "克制",
        "lexical_preferences": ["先看看", "有人看过吗", "反馈多点再说"],
        "emoji_policy": "不用表情",
        "forbidden_expressions": ["冲就完了", "闭眼上", "绝对没问题"],
        "short_prompt_summary": "男性中句聊影视和生活话题，先看多人反馈再追问",
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
