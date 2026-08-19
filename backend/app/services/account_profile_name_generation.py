from __future__ import annotations

import math
import random
import re
from collections import Counter
from dataclasses import dataclass
from typing import Mapping


STYLE_CATEGORIES = (
    "short_nickname",
    "food_taste",
    "animal_persona",
    "scene_place",
    "hobby_object",
    "mood_action",
    "casual_meme",
    "name_like",
    "symbol_variant",
    "daily_phrase",
)
DEFAULT_STYLE_WEIGHTS = {category: 10 for category in STYLE_CATEGORIES}
NAME_GENERATION_MAX_ATTEMPTS = 30_000
MAX_CATEGORY_RATIO = 0.25
MAX_AFFIX_RATIO = 0.05
EMOJI_RE = re.compile(r"[☁️🌙🍀🌻🐾📷🎧🫧🍵]")
COMMON_SURNAMES = frozenset("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕罗毕郝邬安常乐于时傅皮卞齐康伍余元顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁杜阮蓝闵季贾路娄江童颜郭梅林徐邱高夏蔡田樊胡凌霍虞万支柯管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程嵇邢滑裴陆荣翁荀羊甄曲封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀浦尚农温别庄晏柴瞿阎充慕连茹习艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东殴殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")
COMMON_SURNAME_CHOICES = tuple(sorted(COMMON_SURNAMES))

SHORT_STEMS = ("柚", "禾", "满", "九", "朵", "麦", "野", "梨", "栀", "澄", "鹿", "葵", "桃", "醒", "森", "屿", "川", "墨", "眠", "豆", "沐", "念", "宁", "团", "栗", "晚", "初", "圆", "秋", "橙", "岚", "岑", "遥", "言", "也", "一一", "七七", "木子", "北北", "慢慢")
FOODS = ("锅巴", "芋圆", "米线", "青团", "年糕", "豆花", "烤梨", "饭团", "海苔", "薯角", "抹茶", "拿铁", "豆乳", "乌梅", "酸奶", "杏仁", "栗子", "橘子", "柚子", "山楂", "芝麻", "汤圆", "烧麦", "馄饨", "咖喱", "汽水", "麦茶", "椰奶", "可可", "桃酥")
TASTES = ("少糖", "加冰", "不加葱", "多点辣", "趁热吃", "先放凉", "慢慢喝", "刚出锅", "留一口", "不蘸酱", "有点甜", "微微酸", "今天馋", "再来份", "打包走")
ANIMALS = ("橘猫", "黑猫", "白熊", "小狗", "灰兔", "海獭", "松鼠", "刺猬", "企鹅", "浣熊", "海豹", "水豚", "狐狸", "羊驼", "小鹿", "团雀", "夜莺", "雨燕", "鲸鱼", "河马", "考拉", "海鸥", "仓鼠", "小象")
ANIMAL_STATES = ("打盹", "路过", "发呆", "赶车", "看海", "晒背", "躲雨", "收工", "偷闲", "晚睡", "早起", "排队中", "不营业", "慢吞吞", "有点忙", "刚上线")
SCENES = ("巷口", "楼下", "窗边", "站台", "天台", "河堤", "街角", "书店", "花市", "夜班车", "早班地铁", "雨后公园", "周末厨房", "旧唱片店", "山脚小路", "海边长椅", "傍晚球场", "清晨菜场", "午后阳台", "深夜便利店")
SCENE_STATES = ("等一会", "吹吹风", "看看云", "慢慢走", "歇个脚", "等天晴", "刚到站", "准备回家", "还有灯", "路过一下", "坐到天黑", "今天人少", "刚好有空", "听见下雨")
HOBBIES = ("胶片", "跑步", "徒步", "羽毛球", "骑行", "游泳", "烘焙", "手冲", "拼图", "盆栽", "旧书", "漫画", "唱片", "吉他", "键盘", "相机", "耳机", "模型", "象棋", "桌游", "露营", "钓鱼", "电影", "摇滚", "民谣", "篮球", "网球", "书法", "手账", "陶艺")
HOBBY_STATES = ("新手", "练习中", "随便玩玩", "周末选手", "慢慢学", "今天暂停", "下班再说", "偶尔认真", "只看不买", "还在入门", "路过收藏", "有空继续")
MOODS = ("今天不困", "正在放空", "暂时离线", "心情一般", "有点松弛", "刚刚睡醒", "准备早睡", "先吃饭吧", "保持清醒", "不赶时间", "慢慢回复", "随缘上线", "偶尔冒泡", "先收藏着", "正在加载", "周末再聊", "晚点出现", "今天话少", "看完再说", "允许发呆")
MEME_SUBJECTS = ("早睡计划", "减肥进度", "起床气", "周一电量", "下班速度", "摸鱼额度", "社交电量", "今日运气", "记忆容量", "拖延余额", "咖啡浓度", "周末长度", "灵感库存", "购物理智", "闹钟耐心")
MEME_STATES = ("已欠费", "加载失败", "暂未到账", "余额不足", "努力恢复", "不太稳定", "随机刷新", "已经用完", "正在补货", "先缓一缓", "仅供参考", "偶尔在线")
GIVEN_FIRST = ("安", "知", "言", "清", "若", "亦", "景", "星", "念", "予", "书", "时", "简", "宁", "以", "向", "允", "闻", "舒", "南", "初", "乐", "子", "一", "云", "雨", "夏", "秋", "冬", "晨", "晚", "晓", "微", "可", "希", "佳", "思", "沐", "语", "欣")
GIVEN_SECOND = ("然", "宁", "禾", "川", "野", "岚", "遥", "言", "安", "乐", "清", "夏", "秋", "晨", "晚", "舟", "白", "柠", "青", "棠", "竹", "简", "南", "北", "澄", "初", "宜", "一", "予", "念", "冉", "妍", "悦", "彤", "琪", "浩", "宇", "辰", "轩", "泽")
SYMBOLS = ("☁️", "🌙", "🍀", "🌻", "🐾", "📷", "🎧", "🫧", "🍵")
DAILY_SUBJECTS = ("今天的云", "下班以后", "周末清单", "冰箱里的汽水", "还没读完的书", "路边那家小店", "凌晨的末班车", "刚洗好的杯子", "阳台上的风", "忘记带走的伞", "耳机里的旧歌", "排队买到的面包", "窗外的一点雨", "准备收工的人", "还在路上的我")
DAILY_ENDINGS = ("先放这里", "慢慢想吧", "等会再说", "今天算了", "记得带走", "刚好路过", "暂时保密", "留到明天", "不急着回答", "等风小一点", "差点就忘了", "还没想好")


@dataclass(frozen=True)
class GeneratedDisplayName:
    display_name: str
    category: str


@dataclass(frozen=True)
class NameStyleProfile:
    sample_count: int
    weights: tuple[tuple[str, int], ...]
    category_counts: tuple[tuple[str, int], ...]
    length_counts: tuple[tuple[str, int], ...]

    def weight_map(self) -> dict[str, int]:
        return dict(self.weights)


def style_profile_from_names(names: list[str]) -> NameStyleProfile:
    normalized = sorted({name.strip() for name in names if name and name.strip()})
    categories = Counter(classify_display_name(name) for name in normalized)
    lengths = Counter(_length_bucket(name) for name in normalized)
    weights = _bounded_style_weights(categories)
    return NameStyleProfile(
        sample_count=len(normalized),
        weights=tuple(sorted(weights.items())),
        category_counts=tuple(sorted(categories.items())),
        length_counts=tuple(sorted(lengths.items())),
    )


def classify_display_name(name: str) -> str:
    text = name.strip()
    if EMOJI_RE.search(text):
        return "symbol_variant"
    if len(text) >= 7:
        return "daily_phrase"
    if 2 <= len(text) <= 4 and text[0] in COMMON_SURNAMES:
        return "name_like"
    if any(token in text for token in FOODS):
        return "food_taste"
    if any(token in text for token in ANIMALS):
        return "animal_persona"
    if any(token in text for token in HOBBIES):
        return "hobby_object"
    if any(token in text for token in SCENES):
        return "scene_place"
    if any(token in text for token in MEME_SUBJECTS):
        return "casual_meme"
    if len(text) <= 3:
        return "short_nickname"
    return "mood_action"


def generate_unique_display_names(
    count: int,
    unavailable_keys: set[str],
    seed: str,
    *,
    forbidden_words: set[str] | None = None,
    style_weights: Mapping[str, int] | None = None,
    source_name_keys: set[str] | None = None,
) -> list[str]:
    return [
        item.display_name
        for item in generate_display_name_candidates(
            count,
            unavailable_keys,
            seed,
            forbidden_words=forbidden_words,
            style_weights=style_weights,
            source_name_keys=source_name_keys,
        )
    ]


def generate_display_name_candidates(
    count: int,
    unavailable_keys: set[str],
    seed: str,
    *,
    forbidden_words: set[str] | None = None,
    style_weights: Mapping[str, int] | None = None,
    source_name_keys: set[str] | None = None,
) -> list[GeneratedDisplayName]:
    if count < 0:
        raise ValueError("count must not be negative")
    generator = random.Random(seed)
    weights = _validated_weights(style_weights)
    forbidden = {word.strip() for word in (forbidden_words or set()) if word.strip()}
    used = set(unavailable_keys) | set(source_name_keys or set())
    limits = _generation_limits(count)
    results: list[GeneratedDisplayName] = []
    category_counts: Counter[str] = Counter()
    prefix_counts: Counter[str] = Counter()
    suffix_counts: Counter[str] = Counter()
    for _attempt in range(NAME_GENERATION_MAX_ATTEMPTS):
        category = _choose_category(
            generator,
            weights,
            counts=category_counts,
            category_limit=limits[0],
        )
        candidate = _candidate_for_category(generator, category)
        if not _candidate_allowed(
            candidate,
            used=used,
            forbidden=forbidden,
            prefixes=prefix_counts,
            suffixes=suffix_counts,
            affix_limit=limits[1],
        ):
            continue
        results.append(GeneratedDisplayName(candidate, category))
        used.add(_normalize_key(candidate))
        category_counts[category] += 1
        prefix_counts[candidate[:2]] += 1
        suffix_counts[candidate[-2:]] += 1
        if len(results) == count:
            return results
    raise RuntimeError("name_pool_exhausted")


def name_diversity_metrics(items: list[GeneratedDisplayName]) -> dict[str, object]:
    names = [item.display_name for item in items]
    return {
        "category_counts": dict(sorted(Counter(item.category for item in items).items())),
        "length_counts": dict(sorted(Counter(_length_bucket(name) for name in names).items())),
        "max_prefix_count": max(Counter(name[:2] for name in names).values(), default=0),
        "max_suffix_count": max(Counter(name[-2:] for name in names).values(), default=0),
    }


def _bounded_style_weights(counts: Counter[str]) -> dict[str, int]:
    if not counts:
        return dict(DEFAULT_STYLE_WEIGHTS)
    floor = max(1, sum(counts.values()) // 100)
    return {category: min(counts.get(category, 0) + floor, 25 * floor) for category in STYLE_CATEGORIES}


def _validated_weights(style_weights: Mapping[str, int] | None) -> dict[str, int]:
    source = dict(style_weights or DEFAULT_STYLE_WEIGHTS)
    unknown = set(source) - set(STYLE_CATEGORIES)
    if unknown:
        raise ValueError(f"unknown name style categories: {','.join(sorted(unknown))}")
    weights = {category: max(0, int(source.get(category, 0))) for category in STYLE_CATEGORIES}
    if not sum(weights.values()):
        raise ValueError("name style weights must include a positive value")
    return weights


def _generation_limits(count: int) -> tuple[int, int]:
    return max(1, math.ceil(count * MAX_CATEGORY_RATIO)), max(1, math.ceil(count * MAX_AFFIX_RATIO))


def _choose_category(
    generator: random.Random,
    weights: dict[str, int],
    *,
    counts: Counter[str],
    category_limit: int,
) -> str:
    available = [(category, weight) for category, weight in weights.items() if weight and counts[category] < category_limit]
    if not available:
        raise RuntimeError("name_category_capacity_exhausted")
    return generator.choices([item[0] for item in available], weights=[item[1] for item in available], k=1)[0]


def _candidate_allowed(
    candidate: str,
    *,
    used: set[str],
    forbidden: set[str],
    prefixes: Counter[str],
    suffixes: Counter[str],
    affix_limit: int,
) -> bool:
    key = _normalize_key(candidate)
    return bool(
        2 <= len(candidate) <= 12
        and key not in used
        and not any(word in candidate for word in forbidden)
        and prefixes[candidate[:2]] < affix_limit
        and suffixes[candidate[-2:]] < affix_limit
    )


def _candidate_for_category(generator: random.Random, category: str) -> str:
    builders = {
        "short_nickname": _short_name,
        "food_taste": _food_name,
        "animal_persona": _animal_name,
        "scene_place": _scene_name,
        "hobby_object": _hobby_name,
        "mood_action": _mood_name,
        "casual_meme": _meme_name,
        "name_like": _name_like,
        "symbol_variant": _symbol_name,
        "daily_phrase": _daily_phrase,
    }
    return builders[category](generator)


def _short_name(generator: random.Random) -> str:
    stem = generator.choice(SHORT_STEMS)
    return f"{generator.choice(('阿', '小'))}{stem}" if len(stem) <= 2 and generator.random() < 0.7 else stem


def _food_name(generator: random.Random) -> str:
    return f"{generator.choice(FOODS)}{generator.choice(TASTES)}"


def _animal_name(generator: random.Random) -> str:
    return f"{generator.choice(ANIMALS)}{generator.choice(ANIMAL_STATES)}"


def _scene_name(generator: random.Random) -> str:
    return f"{generator.choice(SCENES)}{generator.choice(SCENE_STATES)}"


def _hobby_name(generator: random.Random) -> str:
    return f"{generator.choice(HOBBIES)}{generator.choice(HOBBY_STATES)}"


def _mood_name(generator: random.Random) -> str:
    return generator.choice(MOODS)


def _meme_name(generator: random.Random) -> str:
    return f"{generator.choice(MEME_SUBJECTS)}{generator.choice(MEME_STATES)}"


def _name_like(generator: random.Random) -> str:
    surname = generator.choice(COMMON_SURNAME_CHOICES)
    return f"{surname}{generator.choice(GIVEN_FIRST)}{generator.choice(GIVEN_SECOND)}"


def _symbol_name(generator: random.Random) -> str:
    base = generator.choice(SHORT_STEMS + FOODS + ANIMALS + HOBBIES)
    return f"{base}{generator.choice(SYMBOLS)}"


def _daily_phrase(generator: random.Random) -> str:
    return f"{generator.choice(DAILY_SUBJECTS)}{generator.choice(DAILY_ENDINGS)}"


def _length_bucket(name: str) -> str:
    length = len(name.strip())
    if length <= 3:
        return "short_2_3"
    if length <= 6:
        return "medium_4_6"
    return "long_7_12"


def _normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


__all__ = [
    "GeneratedDisplayName",
    "NameStyleProfile",
    "STYLE_CATEGORIES",
    "classify_display_name",
    "generate_display_name_candidates",
    "generate_unique_display_names",
    "name_diversity_metrics",
    "style_profile_from_names",
]
