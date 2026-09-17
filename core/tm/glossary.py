"""静态术语表匹配 —— 100% 确定性的纯文本算法（零 LLM / 零 token / 零 I/O）。

数据流定位::

    TranslationUnit.extracted_text（已剥离控制符的可译正文）
            │
            ▼
    本模块（最长匹配：同一区间只保留最具体的那条术语）
            │
            ▼
    list[GlossaryMatch]（按首现偏移升序，可直接渲染为术语 Chip）

四条铁律（与 core/lqa/rules.py 同源）：

* 零 LLM / 零 token：全部是字符扫描，结果可复现、可审计；
* 零 I/O / 零网络：不读文件、不开套接字，纯内存只读计算；
* 零全局可变状态：术语表是不可变元组，匹配函数无副作用；
* 失败即返回空：任何输入（None / 空串 / 无命中）都返回 ``[]``，不抛异常。

匹配口径（刻意决策，非遗漏）：

* **最长优先**：同一处起点若既有 ``指切り`` 又有 ``指切りげんまん``，
  取后者 —— 术语表的语义是「这一段必须译成这个」，越具体越可信；
* **不重叠**：被更长匹配完整覆盖的短术语直接丢弃，避免同一段正文
  堆出两条互相矛盾的 Chip；
* **顺序确定**：结果按首现偏移升序，同偏移不可能出现两条（已被不重叠规则排除）。
"""

from collections.abc import Iterator
from typing import Final, NamedTuple


class GlossaryEntry(NamedTuple):
    """一条术语表条目（不可变）。

    ``note`` 供壳层 Chip 上的后缀标记使用，默认 ``Verified`` 表示人工核定；
    机器抽取或低置信度条目应显式标注为 ``Auto`` / ``Tentative`` 之类，
    让译者一眼看出哪些是不可推翻的约束。
    """

    source: str
    """源文（原文侧）术语字面量。"""

    target: str
    """目标译文（术语约束的目标写法）。"""

    note: str = "Verified"
    """置信/来源标记。"""


class GlossaryMatch(NamedTuple):
    """一条命中（不可变）：除术语内容外还带**首现偏移**，供定位与排序。"""

    source: str
    target: str
    note: str
    start: int
    """源文中的起始偏移（含）。"""

    end: int
    """源文中的结束偏移（不含）。"""


GLOSSARY: Final[tuple[GlossaryEntry, ...]] = (
    # —— 通用 Galgame 场景词（跨作品复用率高） ——
    GlossaryEntry("学園祭", "学园祭"),
    GlossaryEntry("文化祭", "文化祭"),
    GlossaryEntry("生徒会", "学生会"),
    GlossaryEntry("幼馴染", "青梅竹马"),
    GlossaryEntry("転校生", "转学生"),
    GlossaryEntry("部活動", "社团活动"),
    GlossaryEntry("制服", "制服"),
    # —— 恋愛線常用抽象名词 ——
    GlossaryEntry("約束", "约定"),
    GlossaryEntry("覚悟", "觉悟"),
    GlossaryEntry("真実", "真相"),
    GlossaryEntry("誠", "诚"),
    # —— 本作固有名词（术语约束优先于通用译法） ——
    GlossaryEntry("千代", "千代", "Character"),
    GlossaryEntry("主人公", "主人公", "Character"),
    GlossaryEntry("指切り", "拉钩"),
    # 与上一条同起点但更长 —— 最长匹配规则的实际靶子
    GlossaryEntry("指切りげんまん", "拉钩上吊"),
    GlossaryEntry("針千本", "一千根针"),
    # —— 高频具象名词 ——
    GlossaryEntry("傘", "伞"),
    GlossaryEntry("雨", "雨"),
    GlossaryEntry("波", "波浪"),
    GlossaryEntry("夜", "夜里"),
)
"""内置术语表。顺序不影响匹配结果（候选会整体排序后再筛选），
但建议保持「通用 → 固有」的书写次序，便于人工审阅。"""


def _occurrences(text: str, entry: GlossaryEntry) -> Iterator[tuple[int, int, GlossaryEntry]]:
    """产出 entry 在 text 中的全部出现区间 ``(start, end, entry)``。

    用 ``str.find`` 游标推进而非正则：术语字面量可能含正则元字符
    （``[``、``(`` 等），转义遗漏会静默漏配，游标法对此天然免疫。
    """
    start = text.find(entry.source)
    while start >= 0:
        end = start + len(entry.source)
        yield start, end, entry
        start = text.find(entry.source, start + 1)


def match_glossary(text: str | None) -> list[GlossaryMatch]:
    """在 text 中匹配术语表，返回按首现偏移升序的命中列表。

    无命中、空串或 None 一律返回 ``[]`` —— 调用方不需要分支判断。
    """
    if not text:
        return []

    candidates: list[tuple[int, int, GlossaryEntry]] = []
    for entry in GLOSSARY:
        candidates.extend(_occurrences(text, entry))

    # 起点升序；**同起点时长者优先**（最长匹配），保证 ‵指切りげんまん‵
    # 胜过同样是起点的 ‵指切り‵。
    candidates.sort(key=lambda item: (item[0], -item[1]))

    chosen: list[GlossaryMatch] = []
    cursor = 0
    for start, end, entry in candidates:
        if start < cursor:
            continue  # 与已选中的区间重叠（被更长的匹配覆盖）→ 丢弃
        chosen.append(
            GlossaryMatch(
                source=entry.source,
                target=entry.target,
                note=entry.note,
                start=start,
                end=end,
            )
        )
        cursor = end
    return chosen
