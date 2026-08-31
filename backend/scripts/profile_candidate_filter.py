from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import GroupContextMessage, Task, TenantLearningProfile, TgAccount, TgGroup


TEACHER_OR_AD_KEYWORDS = (
    "老师", "技师", "课代表", "开课", "上课", "下课", "修车", "品茶", "看图",
    "门牌", "预约", "选妃", "车库", "经济", "经纪", "水头", "空降", "全国空降",
    "嫩妹", "模特", "幼师", "少妇", "极品", "约炮", "约啪", "同城约", "红娘",
    "佳丽", "伴游", "包养", "抓龙筋", "外围", "陪玩", "女仆", "商k", "摸摸唱",
    "快餐", "双飞", "楼凤", "会所", "推油", "spa", "工作室", "体验", "大圈",
    "小圈", "包夜", "出击", "踩雷", "机车", "水车", "照骗", "工兵", "修图",
    "原图", "认证车库", "报告收录", "报告榜", "转运珠", "奶妈", "上岸版",
    "专业69", "69", "上牌", "模版", "打回重写", "最佳艺人", "艺人", "看妹",
    "选妹", "妹子", "小姐姐", "小姐", "妹妹", "白领", "留学生", "兼职", "直营",
    "茶庄", "茶楼", "俱乐部", "招m", "招s", "招募", "私影", "mmk", "影咖",
    "夜班车", "早班车", "出台", "上门", "公寓", "特价", "车评", "车友", "车队",
    "大活", "小活", "过夜", "洗浴", "按摩", "养生", "水疗", "足浴", "足疗",
    "招聘", "招商", "担保", "公群", "汇旺", "供需", "跑分", "承兑", "币币",
    "查档", "定位", "偷拍", "破解", "盘口", "棋牌", "菠菜", "彩票", "出海",
    "换汇", "灰产", "机器人", "bot", "频道", "官频", "唯一客服", "官方",
    "客服", "飞机群", "双向", "单向", "进群", "群组", "交流群", "私聊",
    "详情点击", "戳我", "戳下面", "点击进入", "禁言", "解禁", "进群验证",
    "福利", "资源", "搜索附近", "打车费", "探花", "探店", "管理员", "管理",
    "群主", "防骗", "骗子", "冒充", "假冒", "代充", "退群", "退圈",
    "小心被骗", "飞机号", "打回", "水贴", "中出", "内射", "潮吹", "自慰",
    "做爱", "调教", "足交", "乳交", "口爆", "颜射", "无套", "戴套",
    "包小姐", "包小妹", "援交", "嫖", "母狗",
)
GENERIC_NAMES = frozenset({
    "", "真人用户", "未命名账号", "托管账号", "新托管账号", "已注销账号",
    "deleted account", "telegram", "null", "none", "unknown",
})
OLD_SYNTHETIC_STEMS = frozenset({
    "锅巴", "芋圆", "米线", "青团", "年糕", "豆花", "烤梨", "饭团", "海苔",
    "薯角", "抹茶", "拿铁", "豆乳", "乌梅", "酸奶", "杏仁", "栗子", "橘子",
    "柚子", "山楂", "橘猫", "黑猫", "白熊", "小狗", "灰兔", "海獭", "松鼠",
    "刺猬", "企鹅", "浣熊", "早睡计划", "减肥进度", "起床气", "周一电量",
    "下班速度", "摸鱼额度", "社交电量",
})
OLD_SYNTHETIC_BIOS = frozenset({
    "日常在线，随缘交流", "看到有意思的会回两句", "慢慢看，慢慢聊",
    "偶尔冒泡，不太正式", "记录一点生活碎片", "在线时间不固定，路过就看看",
    "喜欢新鲜事，也喜欢安静围观", "不赶时间，看到合适的话题会接一句",
    "今天也在认真看消息，偶尔分享小想法", "偏随缘的普通用户，熟一点就多聊两句",
    "有空会回，没空就先收藏着", "看见好玩的内容会停一下，顺手聊两句",
    "保持热爱，奔赴山海。", "万物皆有裂痕，那是光照进来的地方。",
    "心如止水，波澜不惊。", "生活明朗，万物可爱。",
    "记录日常点滴。", "行到水穷处，坐看云起时。",
    "慢慢走，沿途皆是风景。", "努力生活，开心每一天。",
    "知足常乐，万事胜意。", "热爱漫无边际，生活自有分寸。",
    "顺其自然，随遇而安。", "平凡之路，步履不停。",
    "今天也要加油鸭！", "人间值得，未来可期。",
    "世界很大，慢慢探索。", "做一个温柔且坚定的人。",
    "风吹又日出，生活很值得。",
})
TEACHER_AD_RE = re.compile(
    "|".join(re.escape(keyword) for keyword in TEACHER_OR_AD_KEYWORDS),
    re.IGNORECASE,
)
ADULT_EMOJIS_RE = re.compile(r"[👙👠🐻🧤💋👄💄👅💦🔞💃👯🍼]")
CONTACT_RE = re.compile(
    r"(t\.me/|https?://|@[a-zA-Z0-9_]{3,}|微[：:\s]*[a-zA-Z0-9_-]{4,}|"
    r"vx[：:\s]*[a-zA-Z0-9_-]{4,}|v[：:\s]*[a-zA-Z0-9_-]{4,}|微信|"
    r"qq[：:\s]*\d{5,}|企鹅[：:\s]*\d{5,}|\+86|\+1|\b1[3-9]\d{9}\b|"
    r"双向[：:\s]*@|唯一账号|联系方式|私信)",
    re.IGNORECASE,
)
SYMBOLS_ONLY = frozenset(".,!?:;-_*~`^#@$%&+=()[]{}<>|/\\'\"• \t\n\r")


@dataclass(frozen=True)
class FilterResult:
    is_valid: bool
    rejection_reason: str = ""

    def __bool__(self) -> bool:
        return self.is_valid


@dataclass(frozen=True)
class GroupCandidateProfile:
    source_type: str
    group_title: str
    group_peer_id: str
    user_id: str
    username: str
    display_name: str
    first_name: str
    last_name: str
    bio: str
    collected_at: str


@dataclass(frozen=True)
class NaturalCandidateProfile:
    group_title: str
    user_id: str
    username: str
    display_name: str
    first_name: str
    last_name: str
    bio: str


class ProfileFilter:
    def __init__(
        self,
        our_account_ids: set[str],
        our_usernames: set[str],
        our_display_names: set[str],
        *,
        task_discussion_teachers: set[str],
        forbidden_words: set[str] | None = None,
    ) -> None:
        self.our_account_ids = {str(val).strip() for val in our_account_ids if val}
        self.our_usernames = {value.lower().strip() for value in our_usernames if value}
        self.our_display_names = {
            self.normalize_name(value) for value in our_display_names if value
        }
        self.task_discussion_teachers = {
            self.normalize_name(value) for value in task_discussion_teachers if value
        }
        self.forbidden_words = {
            self.normalize_name(word) for word in (forbidden_words or set()) if word and word.strip()
        }

    @staticmethod
    def normalize_name(value: str | None) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "").strip())
        return re.sub(r"\s+", " ", normalized).casefold()

    def filter_candidate(
        self,
        user_id: str,
        display_name: str,
        *,
        username: str = "",
        bio: str = "",
        is_bot: bool = False,
        is_deleted: bool = False,
    ) -> FilterResult:
        reason = self._rejection_reason(
            user_id,
            display_name,
            username=username,
            bio=bio,
            is_bot=is_bot,
            is_deleted=is_deleted,
        )
        return FilterResult(not reason, reason)

    def _rejection_reason(
        self,
        user_id: str,
        display_name: str,
        *,
        username: str,
        bio: str,
        is_bot: bool,
        is_deleted: bool,
    ) -> str:
        if is_bot or is_deleted:
            return "is_bot" if is_bot else "is_deleted_account"
        normalized_name = self.normalize_name(display_name)
        normalized_username = username.strip().lower()
        if str(user_id) in self.our_account_ids:
            return "is_our_ai_account (by ID)"
        if normalized_username and normalized_username in self.our_usernames:
            return "is_our_ai_account (by Username)"
        if normalized_name in self.our_display_names:
            return "is_our_ai_account (by Name)"
        if not display_name or normalized_name in GENERIC_NAMES:
            return "generic_placeholder_name"
        if any(stem in display_name for stem in OLD_SYNTHETIC_STEMS):
            return "old_synthetic_name_template"
        if normalized_name in self.task_discussion_teachers:
            return "task_discussion_teacher"
        if self._matches_forbidden(normalized_name):
            return "forbidden_word_in_name"
        name_reason = _unsafe_text_reason(display_name, "name")
        if name_reason:
            return name_reason
        clean_name = display_name.strip()
        if not 1 <= len(clean_name) <= 25:
            return f"invalid_name_length ({len(clean_name)})"
        if clean_name.isdigit() or all(char in SYMBOLS_ONLY for char in clean_name):
            return "invalid_name_content"
        clean_bio = bio.strip()
        if clean_bio:
            normalized_bio = self.normalize_name(clean_bio)
            if self._matches_forbidden(normalized_bio):
                return "forbidden_word_in_bio"
            if any(old_bio in clean_bio for old_bio in OLD_SYNTHETIC_BIOS):
                return "old_synthetic_bio"
            if len(clean_bio) > 120:
                return f"bio_too_long ({len(clean_bio)})"
            return _unsafe_text_reason(clean_bio, "bio")
        return ""

    def _matches_forbidden(self, normalized_text: str) -> bool:
        return any(word in normalized_text for word in self.forbidden_words)


def _unsafe_text_reason(value: str, field: str) -> str:
    if TEACHER_AD_RE.search(value):
        return f"teacher_keyword_in_{field}"
    if ADULT_EMOJIS_RE.search(value):
        return f"adult_emoji_in_{field}"
    if CONTACT_RE.search(value):
        return f"contact_or_link_in_{field}"
    return ""


def load_system_exclusions(
    session: Session,
    tenant_id: int | None = None,
) -> tuple[set[str], set[str], set[str], set[str]]:
    stmt = select(
        TgAccount.id,
        TgAccount.tg_user_id,
        TgAccount.username,
        TgAccount.display_name,
        TgAccount.tg_first_name,
    )
    if tenant_id is not None:
        stmt = stmt.where(TgAccount.tenant_id == tenant_id)
    rows = session.execute(stmt).all()
    account_ids: set[str] = set()
    usernames: set[str] = set()
    names: set[str] = set()
    for row in rows:
        if row[0]:
            account_ids.add(str(row[0]))
        if row[1]:
            account_ids.add(str(row[1]))
        if row[2]:
            usernames.add(row[2].strip())
        if row[3]:
            names.add(row[3].strip())
        if row[4]:
            names.add(row[4].strip())
    teachers = _discussion_teachers(session, tenant_id=tenant_id)
    return account_ids, usernames, names, teachers


def _discussion_teachers(session: Session, tenant_id: int | None = None) -> set[str]:
    teachers: set[str] = set()
    g_stmt = select(TgGroup)
    t_stmt = select(Task)
    p_stmt = select(TenantLearningProfile)
    if tenant_id is not None:
        g_stmt = g_stmt.where(TgGroup.tenant_id == tenant_id)
        t_stmt = t_stmt.where(Task.tenant_id == tenant_id)
        p_stmt = p_stmt.where(TenantLearningProfile.tenant_id == tenant_id)
    for group in session.scalars(g_stmt).all():
        teachers.update(_teacher_names(str(group.topic_direction or "")))
    for task in session.scalars(t_stmt).all():
        for value in (task.type_config or {}).values():
            if isinstance(value, str):
                teachers.update(_teacher_names(value))
    for profile in session.scalars(p_stmt).all():
        summary = getattr(profile, "persona_summary", "") or ""
        teachers.update(re.findall(r"[\u4e00-\u9fa5]{2,6}老师", summary))
    return teachers


def _teacher_names(value: str) -> set[str]:
    match = re.search(r"讨论老师[：:]\s*([^\n;；]+)", value)
    if not match:
        return set()
    return {item for item in re.split(r"[,，、\s]+", match.group(1)) if item}


def extract_group_profiles(
    session: Session,
    profile_filter: ProfileFilter,
    *,
    limit: int = 50,
    tenant_id: int | None = None,
) -> list[NaturalCandidateProfile]:
    max_id = _latest_group_message_id(session, tenant_id)
    stmt = (
        select(
            GroupContextMessage.sender_peer_id,
            GroupContextMessage.sender_name,
            GroupContextMessage.sender_username,
            GroupContextMessage.group_id,
        )
        .join(TgGroup, TgGroup.id == GroupContextMessage.group_id)
        .where(
            GroupContextMessage.id >= max(0, max_id - 50000),
            GroupContextMessage.sender_name.is_not(None),
            GroupContextMessage.sender_name != "",
            GroupContextMessage.is_bot.is_(False),
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(TgGroup.tenant_id == tenant_id)
    stmt = stmt.group_by(
        GroupContextMessage.sender_peer_id,
        GroupContextMessage.sender_name,
        GroupContextMessage.sender_username,
        GroupContextMessage.group_id,
    ).order_by(func.count().desc()).limit(limit * 2)
    return _natural_candidates(
        session,
        stmt,
        profile_filter=profile_filter,
        limit=limit,
        tenant_id=tenant_id,
    )


def _latest_group_message_id(session: Session, tenant_id: int | None) -> int:
    stmt = select(func.max(GroupContextMessage.id))
    if tenant_id is not None:
        stmt = stmt.join(TgGroup, TgGroup.id == GroupContextMessage.group_id).where(
            TgGroup.tenant_id == tenant_id
        )
    return int(session.scalar(stmt) or 0)


def _natural_candidates(
    session: Session,
    stmt,
    *,
    profile_filter: ProfileFilter,
    limit: int,
    tenant_id: int | None,
) -> list[NaturalCandidateProfile]:
    group_stmt = select(TgGroup)
    if tenant_id is not None:
        group_stmt = group_stmt.where(TgGroup.tenant_id == tenant_id)
    group_map = {group.id: group.title for group in session.scalars(group_stmt).all()}
    candidates: list[NaturalCandidateProfile] = []
    seen_names: set[str] = set()
    for row in session.execute(stmt).all():
        candidate = _candidate_from_msg_row(
            row,
            group_map=group_map,
            profile_filter=profile_filter,
            seen_names=seen_names,
        )
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _candidate_from_msg_row(
    row,
    *,
    group_map: dict[int, str],
    profile_filter: ProfileFilter,
    seen_names: set[str],
) -> NaturalCandidateProfile | None:
    display_name = (row.sender_name or "").strip()
    username = (row.sender_username or "").strip()
    peer_id = str(row.sender_peer_id or "")
    if not profile_filter.filter_candidate(user_id=peer_id, display_name=display_name, username=username):
        return None
    name_key = ProfileFilter.normalize_name(display_name)
    if name_key in seen_names:
        return None
    seen_names.add(name_key)
    parts = display_name.split(" ", 1)
    return NaturalCandidateProfile(
        group_title=group_map.get(row.group_id, "Active Group"),
        user_id=peer_id,
        username=username,
        display_name=display_name,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        bio="",
    )


def unique_display_name_from_candidate(
    base_name: str,
    used_keys: set[str],
    seed_idx: int,
    *,
    forbidden_words: set[str] | None = None,
) -> tuple[str, str, str]:
    clean_base = re.sub(r"\s+", " ", base_name.strip())
    forbidden = {ProfileFilter.normalize_name(w) for w in (forbidden_words or set()) if w}
    prefixes = ("小", "阿", "老", "大", "木")
    suffixes = ("呀", "君", "日常", "木木", "呢", "随记")
    natural_words = ("随笔", "行者", "闲客", "听风", "向阳", "漫步", "静思")
    candidates = [clean_base]
    candidates.extend(f"{prefix}{clean_base}" for prefix in prefixes)
    candidates.extend(f"{clean_base}{suffix}" for suffix in suffixes)
    candidates.extend(_compound_name_candidates(clean_base, natural_words, seed_idx))
    for candidate in candidates:
        result = _claim_candidate_name(candidate, used_keys, forbidden)
        if result is not None:
            return result
    raise ValueError("unique_display_name_exhausted")


def _compound_name_candidates(
    base_name: str,
    words: tuple[str, ...],
    seed_idx: int,
) -> list[str]:
    size = len(words)
    return [
        f"{base_name}{words[(seed_idx + offset) % size]}{words[(seed_idx + offset // size + 1) % size]}"
        for offset in range(size * size)
    ]


def _claim_candidate_name(
    candidate: str,
    used_keys: set[str],
    forbidden: set[str],
) -> tuple[str, str, str] | None:
    clean_name = re.sub(r"\s+", " ", candidate.strip())[:25]
    key = ProfileFilter.normalize_name(clean_name)
    if not key or key in used_keys or any(word in key for word in forbidden):
        return None
    used_keys.add(key)
    parts = clean_name.split(" ", 1)
    return clean_name, parts[0], parts[1] if len(parts) > 1 else ""
