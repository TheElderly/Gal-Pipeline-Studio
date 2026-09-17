"""静态 LQA 规则库 —— 100% 确定性纯算法校验。

数据流定位::

    TranslationUnit（模型层：字段级合法）
            │
            ▼
    本模块（跨界一致性断言：标记登记表 ⇄ 原文 / 译文标点规范）
            │
            ▼
    list[LQAIssue]（结构化违例清单；空列表即通过）

模型层（core/models）只能在单字段粒度上设防（非空、非负、类型正确），
而「标记登记表」与「raw_text 字节权威来源」之间的一致性是模型层刻意
留给 LQA 守门狗的跨界断言，由本模块承接。四条铁律：

* 零 LLM / 零 token：全部断言均为字符扫描与计数，结果可复现、可审计；
* 零 I/O / 零网络：不读文件、不开套接字，纯内存只读计算；
* 零全局可变状态：模块常量全部不可变，规则函数无副作用；
* 失败即返回值：违例以 ``LQAIssue`` 列表表达，任何输入都不抛异常，
  流水线凭 severity 分级放行/阻断，不依赖异常控制流。

规则清单（rule_id 与断言内容）：

1. ``atomic_conservation``：原子标记锚点守恒 —— position 落在 raw_text
   界内且区间字面逐字符相等；加强项：已锚定标记的回填区间互不重叠；
2. ``paired_balance``：成对标记配平 —— 开闭标记均存在于 raw_text 且开
   标记首次出现先于闭标记首次出现；加强项：开闭标记在 raw_text 中的
   出现计数配平、inner_text 落于首对开闭区间之内；
3. ``control_conservation``：译文侧控制符携带模式审计（详见
   ``check_control_conservation`` docstring 的三态语义）—— 宏被删改/
   部分携带/篡改参数一律 error 阻断；
4. ``cjk_punctuation``：译文全角标点规范化 —— 直角引号「」配平且无
   交叉错配（栈扫描）、破折号必须以成双「——」出现（禁止孤立单个或
   奇数连续）；辅助提示（warning 级）：省略号 … 应规范化为双三点 ……、
   长破折号 ―（U+2015）应规范化为 ——、半角 ！? 应对齐为全角 ！？。

公开 API：``LQAIssue``、``check_atomic_conservation``、
``check_paired_balance``、``check_control_conservation``、
``check_cjk_punctuation``、``run_static_rules``。
"""

import re
from collections import Counter
from collections.abc import Iterator
from typing import Final, Literal, NamedTuple

from core.models.ir import TranslationUnit

Severity = Literal["error", "warning"]
"""违例严重级别：``"error"`` 阻断导出，``"warning"`` 供人工复核放行。"""


class LQAIssue(NamedTuple):
    """单条静态质检违例。

    不可变轻量载体（NamedTuple：零构造开销、可哈希、可直接参与集合
    运算与 JSON 序列化），实例一经创建即冻结。
    """

    rule_id: str
    """违例所属规则标识，如 ``"atomic_conservation"``。"""

    severity: Severity
    """严重级别，语义见 :data:`Severity`。"""

    message: str
    """中文违例描述，内含单元 id、标记序号、偏移量等定位信息。"""

    unit_id: str | None = None
    """来源翻译单元 id；纯文本级规则被直接调用且未显式提供时为 None。"""


# ---------------------------------------------------------------------------
# 规则标识与字符常量（全部不可变）
# ---------------------------------------------------------------------------

RULE_ATOMIC_CONSERVATION: Final[str] = "atomic_conservation"
"""原子标记锚点守恒规则的 rule_id。"""

RULE_PAIRED_BALANCE: Final[str] = "paired_balance"
"""成对标记配平规则的 rule_id。"""

RULE_CJK_PUNCTUATION: Final[str] = "cjk_punctuation"
"""中日文全角标点规范化规则的 rule_id。"""

RULE_CONTROL_CONSERVATION: Final[str] = "control_conservation"
"""译文侧控制符携带模式审计规则的 rule_id。

命名与 ``atomic_conservation`` 同族（规则语义是「守恒审计」，违例只是
审计结论）—— 命中任何 error 时单元同样被判 ``LQA_FAILED``。
"""

_OPEN_CORNER: Final[str] = "「"  # U+300C LEFT CORNER BRACKET
_CLOSE_CORNER: Final[str] = "」"  # U+300D RIGHT CORNER BRACKET
_EM_DASH: Final[str] = "—"  # U+2014 EM DASH，破折号「——」的组成单元
_HBAR: Final[str] = "―"  # U+2015 HORIZONTAL BAR，须提示规范化为 U+2014 双写
_ELLIPSIS: Final[str] = "…"  # U+2026 HORIZONTAL ELLIPSIS，目标形态为双三点 ……

_HALF_TO_FULL: Final[tuple[tuple[str, str], ...]] = (("!", "！"), ("?", "？"))
"""半角问号/叹号到全角的规范化映射（只读遍历，绝不就地修改）。"""

_CONTROL_TOKEN_RE: Final = re.compile(r"\[[^\[\]\n]{1,64}\]")
"""控制符样式 token 的扫描模式：成对方括号、内部无嵌套括号与换行。

上限 64 字符：真实引擎宏（``[ruby text="…"]`` / ``[font size=24]``）远短于
此；超长的方括号片段几乎必然是正文里的记号（如「参见[注1]」），用长度
护栏避免把整段被误括的散文当成一个 token。
"""


# ---------------------------------------------------------------------------
# 内部扫描原语
# ---------------------------------------------------------------------------


def _scan_runs(text: str, ch: str) -> Iterator[tuple[int, int]]:
    """扫描 text 中字符 ch 的极大连续游程，依次产出 (起始偏移, 游程长度)。

    纯指针推进，无正则、无切片复制，单次线性遍历即可供破折号与省略号
    两条奇偶断言复用。
    """
    i: int = 0
    total: int = len(text)
    while i < total:
        if text[i] == ch:
            j: int = i + 1
            while j < total and text[j] == ch:
                j += 1
            yield i, j - i
            i = j
        else:
            i += 1


# ---------------------------------------------------------------------------
# 规则 1：原子标记锚点守恒
# ---------------------------------------------------------------------------


def check_atomic_conservation(unit: TranslationUnit) -> list[LQAIssue]:
    """断言 unit.atomic_tags 的每个锚点与 raw_text 逐字节吻合。

    逐个标记校验：position 落于 ``[0, len(raw_text) - len(raw_tag)]``
    界内，且 ``raw_text[position : position + len(raw_tag)]`` 与登记的
    raw_tag 逐字符相等。模型层仅对 position 设了 ``ge=0`` 单字段下限，
    「锚点确能取回登记字面」这一跨界一致性由本函数独立复述（防御纵深，
    不依赖调用方是否经由 pydantic 构造路径）。

    加强项（同为纯算法）：对锚点校验通过的标记做回填区间重叠检测 ——
    两个标记的 ``[position, position + len)`` 区间相交意味着逐字节复写
    时互相覆盖，属登记表损坏。

    违例一律 error 级；通过时返回空列表。
    """
    issues: list[LQAIssue] = []
    raw: str = unit.raw_text
    raw_len: int = len(raw)
    anchored: list[tuple[int, int, int, str]] = []
    for idx, tag in enumerate(unit.atomic_tags):
        where: str = f"单元 {unit.id} 第 {idx} 个原子标记 {tag.tag_id!r}"
        start: int = tag.position
        end: int = start + len(tag.raw_tag)
        if start < 0:
            issues.append(
                LQAIssue(
                    rule_id=RULE_ATOMIC_CONSERVATION,
                    severity="error",
                    message=f"{where}：position={start} 为负偏移，锚点非法",
                    unit_id=unit.id,
                )
            )
            continue
        if end > raw_len:
            issues.append(
                LQAIssue(
                    rule_id=RULE_ATOMIC_CONSERVATION,
                    severity="error",
                    message=(
                        f"{where}：锚点区间 [{start}, {end}) 越界"
                        f"（raw_text 长度 {raw_len}，无法取回 {tag.raw_tag!r}）"
                    ),
                    unit_id=unit.id,
                )
            )
            continue
        actual: str = raw[start:end]
        if actual != tag.raw_tag:
            issues.append(
                LQAIssue(
                    rule_id=RULE_ATOMIC_CONSERVATION,
                    severity="error",
                    message=(
                        f"{where}：position={start} 锚点字面不符，"
                        f"登记为 {tag.raw_tag!r}，原文实际为 {actual!r}"
                    ),
                    unit_id=unit.id,
                )
            )
            continue
        anchored.append((start, end, idx, tag.tag_id))
    anchored.sort()
    for left, right in zip(anchored, anchored[1:]):
        if right[0] < left[1]:
            issues.append(
                LQAIssue(
                    rule_id=RULE_ATOMIC_CONSERVATION,
                    severity="error",
                    message=(
                        f"单元 {unit.id}：第 {left[2]} 个原子标记 {left[3]!r} 的"
                        f"回填区间 [{left[0]}, {left[1]}) 与第 {right[2]} 个"
                        f"{right[3]!r} 的 [{right[0]}, {right[1]}) 重叠，"
                        "逐字节复写将互相覆盖"
                    ),
                    unit_id=unit.id,
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 规则 2：成对标记配平
# ---------------------------------------------------------------------------


def check_paired_balance(unit: TranslationUnit) -> list[LQAIssue]:
    """断言 unit.paired_tags 的开闭标记在 raw_text 中存在且次序正确。

    基线断言（error 级）：开标记与闭标记均必须出现于 raw_text；且开标记
    首次出现偏移严格先于闭标记首次出现偏移（先于，含相等即违例 —— 相等
    只可能发生在开闭字面完全相同这一病态登记上）。

    加强项（同为纯算法）：

    * 开闭计数配平（error 级）：``raw_text.count(start_tag)`` 与
      ``raw_text.count(end_tag)`` 必须相等，计数失衡即存在未登记的
      孤残标记；
    * inner_text 区间一致（warning 级）：inner_text 必须出现在首对
      开闭标记夹出的区间 ``raw_text[首开+len(首开) : 首闭]`` 之内，
      否则抽取记录与原文疑似脱钩（advisory，交人工复核）。

    通过时返回空列表。
    """
    issues: list[LQAIssue] = []
    raw: str = unit.raw_text
    for idx, pair in enumerate(unit.paired_tags):
        where: str = f"单元 {unit.id} 第 {idx} 个成对标记"
        first_start: int = raw.find(pair.start_tag)
        first_end: int = raw.find(pair.end_tag)
        if first_start < 0:
            issues.append(
                LQAIssue(
                    rule_id=RULE_PAIRED_BALANCE,
                    severity="error",
                    message=f"{where}：开标记 {pair.start_tag!r} 在 raw_text 中不存在",
                    unit_id=unit.id,
                )
            )
        if first_end < 0:
            issues.append(
                LQAIssue(
                    rule_id=RULE_PAIRED_BALANCE,
                    severity="error",
                    message=f"{where}：闭标记 {pair.end_tag!r} 在 raw_text 中不存在",
                    unit_id=unit.id,
                )
            )
        if first_start < 0 or first_end < 0:
            continue
        if first_start >= first_end:
            issues.append(
                LQAIssue(
                    rule_id=RULE_PAIRED_BALANCE,
                    severity="error",
                    message=(
                        f"{where}：开标记 {pair.start_tag!r} 首次出现于偏移 "
                        f"{first_start}，未先于闭标记 {pair.end_tag!r} 的首次"
                        f"出现偏移 {first_end}，开闭次序颠倒"
                    ),
                    unit_id=unit.id,
                )
            )
            continue
        start_count: int = raw.count(pair.start_tag)
        end_count: int = raw.count(pair.end_tag)
        if start_count != end_count:
            issues.append(
                LQAIssue(
                    rule_id=RULE_PAIRED_BALANCE,
                    severity="error",
                    message=(
                        f"{where}：开标记 {pair.start_tag!r} 在 raw_text 中出现 "
                        f"{start_count} 次，闭标记 {pair.end_tag!r} 出现 "
                        f"{end_count} 次，开闭计数不配平"
                    ),
                    unit_id=unit.id,
                )
            )
        inner_start: int = first_start + len(pair.start_tag)
        interval: str = raw[inner_start:first_end]
        if pair.inner_text not in interval:
            issues.append(
                LQAIssue(
                    rule_id=RULE_PAIRED_BALANCE,
                    severity="warning",
                    message=(
                        f"{where}：inner_text {pair.inner_text!r} 未落在首对"
                        f"开闭标记的区间内（区间实际内容 {interval!r}），"
                        "抽取记录与原文疑似不一致"
                    ),
                    unit_id=unit.id,
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 规则 3：中日文全角标点规范化
# ---------------------------------------------------------------------------


def check_cjk_punctuation(
    text: str | None, unit_id: str | None = None
) -> list[LQAIssue]:
    """断言译文 text 满足全角标点规范化约定；text 为 None 时整条跳过。

    必做断言（error 级）：

    * 直角引号「」配平：以栈扫描核对每个「都有配对的」（反之亦然），
      单一引号种类的交叉错配在该语义下必然表现为「无开可闭」或
      「有开无闭」，两种形态分别定位到具体偏移；
    * 破折号成双：每个极大连续 —（U+2014）游程长度必须为偶数，
      孤立单个与奇数连续（3、5…）一律违例。

    辅助提示（warning 级，规范化 advisory，不阻断）：

    * 省略号 …（U+2026）极大游程为奇数时提示规范化为双三点 ……；
    * 长破折号 ―（U+2015）出现即提示规范化为全角破折号 ——；
    * 半角 ``!`` / ``?`` 出现即提示对齐为全角 ！？。

    通过时返回空列表。
    """
    if text is None:
        return []
    prefix: str = f"单元 {unit_id}：" if unit_id is not None else ""
    issues: list[LQAIssue] = []

    def _add(severity: Severity, message: str) -> None:
        """就地追加一条本规则的违例（闭包内聚公共前缀与 rule_id）。"""
        issues.append(
            LQAIssue(
                rule_id=RULE_CJK_PUNCTUATION,
                severity=severity,
                message=prefix + message,
                unit_id=unit_id,
            )
        )

    open_stack: list[int] = []
    for pos, ch in enumerate(text):
        if ch == _OPEN_CORNER:
            open_stack.append(pos)
        elif ch == _CLOSE_CORNER:
            if open_stack:
                open_stack.pop()
            else:
                _add(
                    "error",
                    f"偏移 {pos} 出现多余的闭直角引号」：当前没有可配对的开引号「",
                )
    for pos in open_stack:
        _add("error", f"偏移 {pos} 的开直角引号「缺少配对的闭引号」")

    for pos, run_len in _scan_runs(text, _EM_DASH):
        if run_len % 2 == 1:
            _add(
                "error",
                f"偏移 {pos} 出现连续 {run_len} 个破折号 —（奇数），"
                f"破折号必须以成双形式「——」出现",
            )

    for pos, run_len in _scan_runs(text, _ELLIPSIS):
        if run_len % 2 == 1:
            _add(
                "warning",
                f"偏移 {pos} 出现连续 {run_len} 个省略号 …（奇数），"
                "应规范化为双三点 ……",
            )

    for pos, ch in enumerate(text):
        if ch == _HBAR:
            _add(
                "warning",
                f"偏移 {pos} 出现长破折号 ―（U+2015），应规范化为全角破折号 ——（两个 U+2014）",
            )

    for pos, ch in enumerate(text):
        for half, full in _HALF_TO_FULL:
            if ch == half:
                _add(
                    "warning",
                    f"偏移 {pos} 出现半角 {half!r}，中文译文应对齐为全角 {full!r}",
                )
    return issues


# ---------------------------------------------------------------------------
# 规则 3：译文侧控制符携带模式审计
# ---------------------------------------------------------------------------


def _token_family(token: str) -> str:
    """取控制符 token 的「族名」：方括号内首个空白/等号前的标识符。

    ``[ruby text="…"]`` → ``ruby``；``[font size=24]`` → ``font``；
    ``[r]`` → ``r``。用于把「参数被改写的已登记宏」与「完全无关的
    方括号记号」区分开 —— 前者是篡改（error），后者只提示（warning）。
    """
    inner = token[1:-1]
    for sep in (" ", "\t", "="):
        index = inner.find(sep)
        if index > 0:
            return inner[:index]
    return inner


def check_control_conservation(unit: TranslationUnit) -> list[LQAIssue]:
    """审计译文的控制符携带模式；text 为 None（未翻译）时整条跳过。

    背景（为什么这条规则是三态的，而不是简单的「译文必须含宏」）::

        当前 KAG 管线的规范形态是「剥离式」：extracted_text 与送翻
        Prompt 都**不含**宏，译文是纯散文，宏由回写器按登记锚点机械
        复写（ir_to_asset）。因此「模型吞宏」在这条管线上构造性不可能。
        真正的风险面是：
        a) 人工在内联编辑框**手动敲入**宏 —— 回写器会再复写一遍，
           造成重复注入（引擎语法错误）；
        b) 译文出现**未登记/被篡改**的控制符 token —— 回写器不认识它，
           会原样送达引擎；
        c) 未来「模型自带宏」的引擎流（Prompt 要求模型保留宏）——
           此时必须校验数量与相对顺序守恒。

    三态语义（按译文中出现的已登记宏字面量占比判定）：

    * **全携带**：每个登记宏字面量在译文中的出现次数与登记数一致，
      且相对顺序与 raw_text 登记序一致 → 通过；
    * **全不携带**（剥离式规范形态）→ 通过；若此时译文里出现方括号
      token：与登记宏同族的判**篡改**（error），无关记号判**未登记**
      （warning，advisory）；
    * **部分携带 / 数量失衡 / 顺序错乱** → error：已携带的宏会被回写器
      二次复写叠加，缺失的由回写器补插 —— 结果既重复又错位，必须阻断。

    全部断言为纯字符串扫描：``Counter`` 计数 + ``str.find`` 游标推进，
    零 LLM、零 I/O，结果可复现。通过时返回空列表。
    """
    text = unit.translated_text
    if text is None:
        return []

    issues: list[LQAIssue] = []
    prefix = f"单元 {unit.id}："

    def _add(severity: Severity, message: str) -> None:
        issues.append(
            LQAIssue(
                rule_id=RULE_CONTROL_CONSERVATION,
                severity=severity,
                message=prefix + message,
                unit_id=unit.id,
            )
        )

    expected_seq: list[str] = [
        tag.raw_tag for tag in sorted(unit.atomic_tags, key=lambda t: t.position)
    ]
    expected_counts: Counter[str] = Counter(expected_seq)
    found_tokens = [
        (match.start(), match.group(0))
        for match in _CONTROL_TOKEN_RE.finditer(text)
    ]
    found_counts: Counter[str] = Counter(token for _, token in found_tokens)

    # 未登记 token：字面量不在登记表内（有登记表且同族 → 篡改 error；
    # 异族或登记表为空 → warning，advisory）
    families = {_token_family(lit) for lit in expected_counts}
    for _, token in found_tokens:
        if token in expected_counts:
            continue
        if _token_family(token) in families:
            _add(
                "error",
                f"译文出现登记宏的篡改变体 {token!r}（同族控制符但参数/字面与"
                f"登记不符），回写锚点按登记字面量匹配，篡改变体将被原样送达引擎",
            )
        else:
            _add(
                "warning",
                f"译文出现未登记的控制符样式 token {token!r}，回写器不识别它，"
                "将原样送入引擎 —— 若确需该标记请先在抽取层登记",
            )

    if not expected_counts:
        return issues

    if all(found_counts[literal] == 0 for literal in expected_counts):
        return issues  # 剥离式规范形态：宏全部由回写器机械复写

    missing = [lit for lit in expected_counts if found_counts[lit] == 0]
    if missing:
        carried = [lit for lit in expected_counts if found_counts[lit] > 0]
        _add(
            "error",
            f"译文仅部分携带控制宏：已携带 {carried!r}，缺失 {missing!r}。"
            "回写器会把全部登记宏按锚点复写进译文，已携带部分将叠加成重复注入、"
            "缺失部分看似由回写补齐实则位置失锚 —— 请要么删除手动敲入的宏，"
            "要么完整携带全部宏",
        )
        return issues

    over = [
        lit for lit, count in expected_counts.items() if found_counts[lit] > count
    ]
    if over:
        _add(
            "error",
            f"译文携带的控制宏数量超出登记：{over!r}，回写器复写后必然重复注入",
        )
        return issues

    # 数量全部一致 → 游标推进校验相对顺序（与 raw_text 登记序一致）
    cursor = 0
    for literal in expected_seq:
        index = text.find(literal, cursor)
        if index < 0:
            _add(
                "error",
                f"控制宏 {literal!r} 在译文中的相对顺序与登记序不符"
                f"（游标已推进至 {cursor}，其前必须出现的宏已消费完毕）",
            )
            break
        cursor = index + len(literal)
    return issues


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------


def run_static_rules(unit: TranslationUnit) -> list[LQAIssue]:
    """对单个翻译单元依次跑全部静态规则并聚合违例。

    违例顺序稳定可复现：atomic_conservation → paired_balance →
    control_conservation → cjk_punctuation；同一规则内部按输入登记顺序 /
    扫描偏移顺序排列。
    译文为 None（尚未翻译）时标点与控制符携带规则整条自然跳过，其余规则照常生效。
    """
    issues: list[LQAIssue] = []
    issues.extend(check_atomic_conservation(unit))
    issues.extend(check_paired_balance(unit))
    issues.extend(check_control_conservation(unit))
    issues.extend(check_cjk_punctuation(unit.translated_text, unit_id=unit.id))
    return issues
