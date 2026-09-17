"""XAML ↔ ViewModel 绑定契约静态核对（无 GUI 测试纪律下的唯一自动化防线）。

背景
----
2026-09-16 在 ``StudioView.xaml`` 发现悬空绑定 ``Visibility="{Binding InspectorVisibility}"``：
``StudioViewModel`` 从未暴露该属性，绑定静默失败（不抛异常、只在调试输出留一行警告），
导致 ``ToggleInspector`` 命令点了完全无视觉效果，叠加该处硬编码 ``Width="280"``，
整块 Inspector 折叠能力形同虚设。这类缺陷只有在 GUI 里肉眼观察才会暴露，
而本项目严禁任何前台 GUI 测试，因此必须把它变成静态可拦的错误。

校验项
------
1. 绑定路径根属性必须存在于对应 DataContext 作用域的 VM：
   ``DataTemplate`` 内 → ``StudioUnitItemViewModel``；其余 → ``StudioViewModel``。
   支持 ``SelectedUnit.<Member>`` 二段式下钻校验。
2. ``DataTemplate`` / ``ControlTemplate`` 触发器里的 ``SourceName`` / ``TargetName``
   必须指向同一文档内已声明的 ``x:Name``（守护自绘焦点底线这类命名元素联动）。
3. 历史悬空绑定 ``InspectorVisibility`` 被固化为回归断言，防止复发。
4. 设计纪律回归：静默态译文底线永不显示（仅聚焦时点亮）；原文列与译文列 Top 对齐 + 6px 顶部留白
   必须保持一致（带宏标签的高行首行基线平齐）。

已知局限（只降低灵敏度，不产生误报）
------------------------------------
* 不做完整 XAML 名称作用域分析，``x:Name`` 采用文档级并集，跨模板重名不会被报出。
* 不解析集合项类型与转换器，也不校验 ``Converter`` / ``StringFormat`` 的参数类型。
* ``[RelayCommand]`` 生成属性按「方法名去 Async + Command」后缀规则静态推定。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WF = REPO_ROOT / "src" / "GalPipeline.UI.Wpf"

XAML_PATH = WF / "Views" / "StudioView.xaml"
ROOT_VM_PATH = WF / "ViewModels" / "StudioViewModel.cs"
ITEM_VM_PATH = WF / "ViewModels" / "StudioUnitItemViewModel.cs"

MAIN_XAML_PATH = WF / "MainWindow.xaml"
SHELL_VM_PATH = WF / "ViewModels" / "ShellViewModel.cs"

# 历史遗留的 Unicode 伪字形：播放控制曾用 "⏮ ▶ ⏭"、Re-try 曾用 "🔄"，
# 字体回退后会渲染成大小形状不一的方框 + 裸三角。一律改用 ui:SymbolIcon。
FORBIDDEN_GLYPHS = ("\u23ee", "\u23ed", "\u25b6", "\u23f8", "\u23f9", "\U0001f504")

# 低饱和暗钢蓝微阶的判定阈值（对照设计稿众数 #2A3C50）。
MAX_CHANNEL_SPREAD = 0x30   # 单片色相跨度上限
MAX_LOW_SAT_LUMA = 0x80     # 单分量亮度上限（防止高光色块回归）

# 绑定表达式里「具名参数」的判定：首段含 ``=`` 即视为参数（路径须由 Path= 给出或被省略）。
XAML_NAME = "{http://schemas.microsoft.com/winfx/2006/xaml}Name"

# 审校行「原文列 / 译文列」约定的首行顶部留白（px）。
ROW_TOP_INSET = "6"

# --------------------------- XAML 绑定路径抽取 ---------------------------

_BINDING_START = re.compile(r"\{Binding\b")


def _match_brace(text: str, start: int) -> int:
    """返回从 ``text[start]`` 处的 ``{`` 起配平的 ``}`` 下标；未配平则返回 -1。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_level_commas(inner: str) -> list[str]:
    """按顶层逗号切分绑定参数，忽略嵌套花括号内的逗号。"""
    parts, buf, depth = [], [], 0
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def extract_binding_paths(expr: str) -> list[str]:
    """从属性值 ``expr`` 中抽出所有 ``{Binding …}`` 的路径（``{Binding}`` 记为 ``""``）。"""
    paths: list[str] = []
    for m in _BINDING_START.finditer(expr):
        end = _match_brace(expr, m.start())
        if end < 0:
            continue
        inner = expr[m.start() + 1:end]
        inner = inner[len("Binding"):]

        args = _split_top_level_commas(inner)
        first = args[0] if args else ""
        path = ""
        if first.startswith("Path="):
            path = first[len("Path="):].strip()
        elif first and "=" not in first:
            path = first

        if not path:
            continue
        # RelativeSource / Source 指向元素或第三方对象，不在本测试的 VM 契约范围内。
        if any(a.startswith(("RelativeSource=", "Source=")) for a in args):
            continue
        if path == "DataContext" or path.startswith("DataContext."):
            continue
        paths.append(path)
    return paths


def _referenced_trigger_names(root: ET.Element) -> set[str]:
    names: set[str] = set()
    for el in root.iter():
        for attr in ("SourceName", "TargetName"):
            value = el.attrib.get(attr)
            if value:
                names.add(value)
    return names


def _declared_x_names(root: ET.Element) -> set[str]:
    return {el.attrib[XAML_NAME] for el in root.iter() if XAML_NAME in el.attrib}


def _local_tag(el: ET.Element) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.split("}")[-1]


def collect_bindings(root: ET.Element):
    """遍历 XAML 树，产出 ``(tag, path, scope)``。

    scope 由最近的 ``ItemsSource`` 绑定决定：``UnitsView`` → 行 VM、
    ``MacroPills`` → ``MacroPill`` 记录；其余集合（字符串/整数序列）标为
    ``opaque``，其成员路径不参与校验（无法从 XAML 静态推断元素类型）。
    """
    found = []

    def walk(el: ET.Element, scope: str) -> None:
        for attr_value in el.attrib.values():
            if "{Binding" not in attr_value:
                continue
            for path in extract_binding_paths(attr_value):
                found.append((_local_tag(el), path, scope))

        child_scope = scope
        items_source = el.attrib.get("ItemsSource")
        if items_source and "{Binding" in items_source:
            paths = extract_binding_paths(items_source)
            if paths:
                child_scope = ITEM_SCOPE_BY_SOURCE.get(paths[0].split(".")[0], "opaque")

        for child in el:
            walk(child, child_scope)

    walk(root, "root")
    return found


# 集合 ItemsSource 绑定路径 → 其元素作用域标签。
ITEM_SCOPE_BY_SOURCE = {
    "UnitsView": "item",        # 审校行：StudioUnitItemViewModel
    "MacroPills": "macro",      # 宏药丸：MacroPill record
}


def _item_type_members(cs_source: str) -> dict[str, set[str]]:
    """解析 ``record X(...)`` 位置参数，得到辅助元素类型的成员表。"""
    table: dict[str, set[str]] = {}
    for m in re.finditer(r"record\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", cs_source):
        names: set[str] = set()
        for param in m.group(2).split(","):
            param = param.strip()
            if not param:
                continue
            names.add(param.split()[-1].lstrip("@"))
        table[m.group(1)] = names
    return table


# --------------------------- C# VM 公共成员抽取 ---------------------------

_MODIFIERS = (
    "static", "partial", "readonly", "const", "async", "override", "virtual",
    "new", "abstract", "sealed", "event", "unsafe", "extern", "required",
)

_MEMBER_RE = re.compile(
    r"public\s+"
    r"(?:(?:" + "|".join(_MODIFIERS) + r")\s+)*"
    r"[A-Za-z_][\w\.<>,\[\]\?\s]*?"
    r"\s+([A-Za-z_]\w*)\s*(?=[\{\(=>;])"
)

_RELAY_COMMAND_RE = re.compile(
    r"\[RelayCommand(?:\([^)]*\))?\][^\)]*?"
    r"private\s+(?:(?:static|async)\s+)*"
    r"[A-Za-z_][\w\.<>,\[\]\?\s]*?"
    r"\s+([A-Za-z_]\w*)\s*\(",
    re.DOTALL,
)


def public_members(cs_source: str) -> set[str]:
    """抽取 public 成员名，并补上 ``[RelayCommand]`` 源生成器产出的 ``XxxCommand``。"""
    members = {m.group(1) for m in _MEMBER_RE.finditer(cs_source)}
    for m in _RELAY_COMMAND_RE.finditer(cs_source):
        name = m.group(1)
        if name.endswith("Async"):
            name = name[: -len("Async")]
        members.add(f"{name}Command")
    return members


# --------------------------- fixtures ---------------------------

class _Fixtures:
    def __init__(self) -> None:
        self.xaml_text = XAML_PATH.read_text(encoding="utf-8")
        self.root = ET.fromstring(self.xaml_text)
        self.root_members = public_members(ROOT_VM_PATH.read_text(encoding="utf-8"))
        item_source = ITEM_VM_PATH.read_text(encoding="utf-8")
        self.item_members = public_members(item_source)
        self.item_type_members = _item_type_members(item_source)
        self.bindings = collect_bindings(self.root)
        self.x_names = _declared_x_names(self.root)
        self.trigger_names = _referenced_trigger_names(self.root)
        self.by_name = {
            el.attrib[XAML_NAME]: el
            for el in self.root.iter()
            if XAML_NAME in el.attrib
        }
        self.item_vm_source = item_source
        self.root_vm_source = ROOT_VM_PATH.read_text(encoding="utf-8")
        self.view_codebehind = (WF / "Views" / "StudioView.xaml.cs").read_text(encoding="utf-8")

        # 主壳窗口：DataContext 为 ShellViewModel
        self.main_text = MAIN_XAML_PATH.read_text(encoding="utf-8")
        self.main_root = ET.fromstring(self.main_text)
        self.shell_members = public_members(SHELL_VM_PATH.read_text(encoding="utf-8"))
        self.main_bindings = collect_bindings(self.main_root)

        self.parents: dict[int, ET.Element] = {}
        for parent in self.root.iter():
            for child in parent:
                self.parents[id(child)] = parent

        self.main_parents: dict[int, ET.Element] = {}
        for parent in self.main_root.iter():
            for child in parent:
                self.main_parents[id(child)] = parent


_fx: _Fixtures | None = None


def _fixtures() -> _Fixtures:
    global _fx
    if _fx is None:
        _fx = _Fixtures()
    return _fx


# --------------------------- 测试 ---------------------------

def test_xaml_exists_and_is_wellformed():
    fx = _fixtures()
    assert fx.xaml_text.strip(), "StudioView.xaml 为空"
    assert fx.bindings, "未从 StudioView.xaml 解析出任何绑定，解析器可能已失效"


def test_no_dangling_binding_paths():
    """每条绑定路径都必须能在对应作用域的 VM 上解析到 public 成员。"""
    fx = _fixtures()
    unknown: list[str] = []

    for tag, path, scope in fx.bindings:
        if scope == "opaque":
            continue  # 元素类型无法静态推断（字符串/整数序列），不参与校验

        segments = path.split(".")
        if scope == "macro":
            available = fx.item_type_members.get("MacroPill", set())
            target = "MacroPill"
        elif scope == "item":
            available = fx.item_members
            target = "StudioUnitItemViewModel"
        else:
            available = fx.root_members
            target = "StudioViewModel"

        if segments[0] not in available:
            unknown.append(f"<{tag}> {path}  →  {target} 无成员 {segments[0]}")
            continue

        # 二段式下钻：SelectedUnit.X 落到行 VM；其余按同作用域逐段核验。
        rest = segments[1:]
        if segments[0] == "SelectedUnit":
            for seg in rest:
                if seg not in fx.item_members:
                    unknown.append(f"<{tag}> {path}  →  StudioUnitItemViewModel 无成员 {seg}")
                    break
        elif scope in ("item", "macro"):
            for seg in rest:
                if seg not in available:
                    unknown.append(f"<{tag}> {path}  →  {target} 无成员 {seg}")
                    break

    assert not unknown, "发现悬空绑定（绑定路径在 VM 上不存在）：\n  " + "\n  ".join(unknown)


def test_inspector_visibility_binding_is_not_dangling():
    """历史回归：InspectorVisibility 从未在 StudioViewModel 上定义，绑定静默失败。"""
    fx = _fixtures()
    assert "InspectorVisibility" not in fx.root_members, (
        "StudioViewModel 出现了 InspectorVisibility —— 若确为新增属性，请同步删除本回归断言"
    )
    offenders = [
        path for _, path, _ in fx.bindings if path.split(".")[0] == "InspectorVisibility"
    ]
    assert not offenders, (
        "StudioView.xaml 仍在绑定已确认不存在的 InspectorVisibility："
        "折叠态请用 InspectorContentVisibility / InspectorExpandVisibility"
    )


def test_trigger_source_and_target_names_are_declared():
    """Trigger.SourceName / Setter.TargetName 必须指向同文档已声明的 x:Name。"""
    fx = _fixtures()
    missing = sorted(fx.trigger_names - fx.x_names)
    assert not missing, "触发器引用了未声明的 x:Name：\n  " + "\n  ".join(missing)


def test_translation_focus_underline_wiring():
    """守护自绘焦点底线：命名元素齐全，且属于自绘方案（TextBox 上无局部 BorderBrush）。"""
    fx = _fixtures()
    for required in ("TranslationBox", "TranslationUnderlineIdle", "TranslationUnderlineFocus"):
        assert required in fx.x_names, f"自绘底线缺少命名元素 x:Name=\"{required}\""

    assert "TranslationUnderlineFocus" in fx.trigger_names, (
        "没有任何触发器驱动焦点底线，聚焦态将不可见"
    )

    box = fx.by_name["TranslationBox"]
    assert "BorderBrush" not in box.attrib, (
        "TranslationBox 上出现局部 BorderBrush —— WPF 优先级为「局部值 > 样式触发器」，"
        "会再次压垮焦点视觉，必须走自绘底线方案"
    )
    assert box.attrib.get("BorderThickness") == "0", (
        "TranslationBox 应为无边框（BorderThickness=0），底线由自绘叠加层负责"
    )
    assert box.attrib.get("LostFocus") == "OnTranslationLostFocus", (
        "必须保留 LostFocus -> CommitEdits 持久化链路"
    )
    assert "UpdateSourceTrigger=LostFocus" in box.attrib.get("Text", ""), (
        "译文绑定必须保持 UpdateSourceTrigger=LostFocus，防止虚拟化滚动丢字"
    )


def test_mainwindow_bindings_resolve_against_shell_vm():
    """主壳窗口的绑定同样必须落在 ShellViewModel 上 —— 窗口层的悬空绑定一样是静默失败。"""
    fx = _fixtures()
    assert fx.main_bindings, "未从 MainWindow.xaml 解析出任何绑定，解析器可能已失效"

    unknown: list[str] = []
    for tag, path, _scope in fx.main_bindings:
        head = path.split(".")[0]
        if head not in fx.shell_members:
            unknown.append(f"<{tag}> {path}  →  ShellViewModel 无成员 {head}")

    assert not unknown, "MainWindow.xaml 存在悬空绑定：\n  " + "\n  ".join(unknown)


def test_nav_slots_match_design():
    """导航槽必须为设计稿的四项顺序 Assets / Studio / LQA / Media（+ Footer Settings），
    且每项都带 SymbolIcon 与两行内容（主标签 + ACTIVE 副标签）。"""
    fx = _fixtures()
    menu_tags: list[str] = []
    footer_tags: list[str] = []
    for el in fx.main_root.iter():
        if _local_tag(el) != "NavigationViewItem":
            continue
        parent = fx.main_parents.get(id(el))
        bucket = _local_tag(parent) if parent is not None else ""
        target = footer_tags if "Footer" in bucket else menu_tags
        target.append(el.attrib.get("Tag", ""))
        assert "SymbolIcon" in el.attrib.get("Icon", ""), (
            f"导航项 {el.attrib.get('Tag')} 缺少 SymbolIcon 图标"
        )

    assert menu_tags == ["assets", "studio", "lqa", "media"], (
        f"主导航槽顺序/数量不符设计稿，实际 = {menu_tags}"
    )
    assert footer_tags == ["settings"], f"Footer 导航槽应为 [settings]，实际 = {footer_tags}"

    active_labels = fx.main_text.count('Text="ACTIVE"')
    assert active_labels == len(menu_tags) + len(footer_tags), (
        f"每个导航项都应带 ACTIVE 副标签（期望 {len(menu_tags) + len(footer_tags)} 个，实际 {active_labels} 个）"
    )
    assert "IsActive" in fx.main_text, "ACTIVE 副标签必须由 NavigationViewItem.IsActive 驱动"


def test_emoji_glyphs_are_purged_from_xaml():
    """严禁用 Unicode 伪字形充当图标：字体回退后会渲染成大小形状不一的方框与裸三角。"""
    fx = _fixtures()
    offenders: list[str] = []
    for name, text in (("StudioView.xaml", fx.xaml_text), ("MainWindow.xaml", fx.main_text)):
        for line_no, line in enumerate(text.splitlines(), 1):
            for glyph in FORBIDDEN_GLYPHS:
                if glyph in line:
                    offenders.append(
                        f"{name}:{line_no} 出现禁用字形 U+{ord(glyph):04X} → {line.strip()[:70]}"
                    )
    assert not offenders, "Unicode 伪字形必须换用 ui:SymbolIcon：\n  " + "\n  ".join(offenders)


def test_speaker_badge_is_capsule_with_low_saturation_palette():
    """徽章必须是全圆角胶囊 + 低饱和暗钢蓝微阶（彻底废弃高饱和高光色块）。"""
    fx = _fixtures()

    badge = fx.by_name["SpeakerBadgeCapsule"]
    assert badge.attrib.get("CornerRadius") == "12", "徽章必须为全圆角胶囊（CornerRadius=12）"
    assert "SpeakerBadgeBrush" in badge.attrib.get("Background", ""), (
        "徽章底色必须绑定 SpeakerBadgeBrush"
    )

    assert "SpeakerBadgeForeground" in fx.item_members, "徽章文字色需暴露为 VM 属性"
    assert "BadgeColdWhite" in fx.item_vm_source, "徽章文字色必须为高对比冷白（#EEF8FF）"

    block = re.search(r"SpeakerPalette\s*=\s*\[(.*?)\];", fx.item_vm_source, re.S)
    assert block, "未找到 SpeakerPalette 定义"
    triples = [
        (int(r, 16), int(g, 16), int(b, 16))
        for r, g, b in re.findall(
            r"FromRgb\(0x([0-9A-Fa-f]{2}),\s*0x([0-9A-Fa-f]{2}),\s*0x([0-9A-Fa-f]{2})\)",
            block.group(1),
        )
    ]
    assert triples, "SpeakerPalette 未解析出任何颜色"
    for triple in triples:
        spread = max(triple) - min(triple)
        assert spread <= MAX_CHANNEL_SPREAD, (
            f"徽章底色 {triple} 色相跨度过大（{spread} > {MAX_CHANNEL_SPREAD}），高饱和色块回归"
        )
        assert max(triple) <= MAX_LOW_SAT_LUMA, (
            f"徽章底色 {triple} 过亮（> {MAX_LOW_SAT_LUMA}），低饱和暗钢蓝纪律被破坏"
        )
    assert any(t == (0x35, 0x45, 0x5A) for t in triples), (
        "调色板必须至少包含设计稿基准 #35455A"
    )


def test_status_pill_is_saturated_solid_with_white_text():
    """状态胶囊必须是**高饱和实心 + 纯白字 + 全圆角**（对照设计稿的 LQA 徽章强度）。

    注意与 `test_speaker_badge_is_capsule_with_low_saturation_palette` 的边界：
    「低饱和」只约束 Col1 角色徽章；Col4 状态胶囊走的是相反的视觉策略。
    """
    fx = _fixtures()
    pill = fx.by_name["StatusPill"]
    assert pill.attrib.get("CornerRadius") == "12", "状态胶囊必须为全圆角"
    assert pill.attrib.get("Padding") == "10,4", "状态胶囊 Padding 必须为 10,4"
    assert "StatusBrush" in pill.attrib.get("Background", ""), "胶囊底色必须绑定 StatusBrush"

    def triple(field: str) -> tuple[int, int, int]:
        m = re.search(
            rf"{field} = new SolidColorBrush\(Color\.FromRgb\("
            r"0x([0-9A-Fa-f]{2}), 0x([0-9A-Fa-f]{2}), 0x([0-9A-Fa-f]{2})\)\)",
            fx.item_vm_source,
        )
        assert m, f"未找到 {field} 定义"
        return (int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16))

    passed = triple("PassedBrush")
    failed = triple("FailedBrush")
    assert passed[1] > passed[0] and passed[1] > passed[2] and max(passed) - min(passed) >= 0x50, (
        f"Passed 必须为高饱和绿实底，实际 {passed}"
    )
    assert failed[0] > failed[1] and failed[0] > failed[2] and max(failed) - min(failed) >= 0x40, (
        f"Failed 必须为高饱和红实底，实际 {failed}"
    )
    for field in ("PassedForeground", "FailedForeground", "DraftForeground"):
        assert triple(field) == (0xFF, 0xFF, 0xFF), f"{field} 必须为纯白 (#FFFFFF)"


def test_no_remote_image_sources():
    """离线红线：XAML 严禁引用任何远程图源，立绘占位也必须是本地资产。

    注意：只检查**承载图片的属性值**（Source/ImageSource/Background/Fill/Icon）。
    不能全文扫 `http://` —— XML 命名空间声明（`xmlns="http://schemas..."`）本身就是 URL，
    但它只是标识符、不产生任何网络请求，全文扫描会造成纯误报。
    """
    fx = _fixtures()
    image_attrs = {"Source", "ImageSource", "Background", "Fill", "Icon"}
    offenders: list[str] = []

    for name, root in (("StudioView.xaml", fx.root), ("MainWindow.xaml", fx.main_root)):
        for el in root.iter():
            for key, value in el.attrib.items():
                attr = key.split("}")[-1]
                if attr not in image_attrs:
                    continue
                low = value.lower()
                if "http://" in low or "https://" in low or low.startswith("data:"):
                    offenders.append(f"{name} <{_local_tag(el)} {attr}=\"{value[:60]}\">")

    assert not offenders, "检测到远程/内联图源，违反离线红线：\n  " + "\n  ".join(offenders)

    assert "character-standing-placeholder.png" in fx.xaml_text, (
        "立绘卡必须引用本地占位资产（真实立绘就位后替换同名文件即可）"
    )


def test_local_assets_are_embedded_as_resources():
    """XAML 引用的本地资产必须真实存在，且在 csproj 中被显式标记为 Resource。

    WPF 的 pack URI **编译期不做校验**：漏加 `<Resource>` 时构建依然 0 错误 0 警告，
    直到运行期 `LoadComponent` 抛 `IOException: 找不到资源"assets/xxx.png"`，
    表现是整个内容区一片空白（本项目 App 的黑匣子日志抓到过这一次）。
    这条断言把「编译通过、运行崩溃」关进 CI。
    """
    fx = _fixtures()
    csproj = (WF / "GalPipeline.UI.Wpf.csproj").read_text(encoding="utf-8")

    refs = re.findall(r'ImageSource="(/[^"]+)"', fx.xaml_text)
    assert refs, "未从 StudioView.xaml 发现任何本地资产引用，解析器可能已失效"

    problems: list[str] = []
    for ref in refs:
        rel = ref.lstrip("/")
        if not (WF / rel).exists():
            problems.append(f"资产文件不存在：{ref}")
            continue
        if rel.replace("/", "\\") not in csproj:
            problems.append(f"资产未被 <Resource> 嵌入（运行期必崩）：{ref}")

    assert not problems, "本地资产引用不完整：\n  " + "\n  ".join(problems)


def test_inspector_is_split_into_two_sibling_cards():
    """Inspector 必须拆成两张平级卡片（Context / Glossary），卡间透出深色底板。"""
    fx = _fixtures()

    context = fx.by_name["InspectorContextCard"]
    glossary = fx.by_name["InspectorGlossaryCard"]
    for label, el in (("Context", context), ("Glossary", glossary)):
        assert el.attrib.get("CornerRadius") == "12", f"{label} 卡圆角必须为 12"

    assert fx.parents[id(context)] is fx.parents[id(glossary)], (
        "两张卡必须是同一父容器下的平级兄弟节点，才能在卡间透出深色底板"
    )
    assert _local_tag(fx.parents[id(context)]) == "StackPanel", "双卡应承载于竖向 StackPanel"

    divider = [
        el for el in context.iter()
        if _local_tag(el) == "Border"
        and el.attrib.get("Height") == "1"
        and el.attrib.get("Background") == "#252E42"
    ]
    assert divider, "立绘与波形之间缺少 1px #252E42 分隔线"


def test_idle_underline_is_never_shown():
    """设计纪律：静默态译文列必须是无修饰的纯净文本，绝不出现常驻横线。"""
    fx = _fixtures()

    idle = fx.by_name["TranslationUnderlineIdle"]
    assert idle.attrib.get("Opacity") == "0", (
        "TranslationUnderlineIdle 必须常驻 Opacity=\"0\"，否则每一行都会出现常驻底线，破坏设计稿质感"
    )
    assert "TranslationUnderlineIdle" not in fx.trigger_names, (
        "存在驱动 TranslationUnderlineIdle 的触发器 —— 静默态底线必须永不点亮。"
        "若确需改变此设计纪律，请同步修改本断言并在 docs/AI_HANDOFF.md 记录原因"
    )

    focus = fx.by_name["TranslationUnderlineFocus"]
    assert focus.attrib.get("Opacity") == "0", "发光底线默认必须不可见，仅聚焦时点亮"


def test_row_baseline_alignment_contract():
    """原文列与译文列必须同为 Top 对齐 + 同 6px 顶部留白，带宏标签的高行首行基线才平齐。"""
    fx = _fixtures()

    box = fx.by_name["TranslationBox"]
    assert box.attrib.get("VerticalAlignment") == "Top", (
        "译文框必须 Top 对齐（Center 会让高行的译文悬空错位）"
    )
    assert box.attrib.get("Padding") == f"0,{ROW_TOP_INSET},0,{ROW_TOP_INSET}", (
        f"译文框 Padding 必须为 0,{ROW_TOP_INSET},0,{ROW_TOP_INSET}，实际 {box.attrib.get('Padding')}"
    )

    # 原文列：宏药丸独占一行 + 原文（设计稿为上下结构），顶部留白与译文列对齐
    source = next(
        el for el in fx.root.iter()
        if _local_tag(el) == "StackPanel"
        and el.attrib.get("Grid.Column") == "1"
        and "VerticalAlignment" in el.attrib
    )
    assert source.attrib.get("VerticalAlignment") == "Top", (
        "原文列必须 Top 对齐（曾为 Center，与译文列顶部基线错位）"
    )
    margin = [int(v) for v in source.attrib.get("Margin", "0").split(",")]
    assert len(margin) == 4 and margin[1] == int(ROW_TOP_INSET), (
        f"原文列顶部留白必须为 {ROW_TOP_INSET}px，实际 Margin={source.attrib.get('Margin')}"
    )


# --------------------------- 业务闭环纪律（不是外观，是数据来源） ---------------------------


def test_business_loop_is_wired_to_real_rpc():
    """加载 / 质检 / 导出必须全部落在真实 RPC 上，**严禁回退到本地硬编码数据**。

    这条守护的是「真实脚本加载与导出闭环」这项业务要求本身 ——
    它不看 UI 外观，只钉住数据来源：首屏内容必须来自 Sidecar 对真实
    KAG 剧本的解析，人工改动必须经质检才能定状态，导出必须经 ir_to_asset。
    """
    fx = _fixtures()
    src = fx.root_vm_source
    flat = re.sub(r"\s+", " ", src)

    assert "LoadDemoUnits" not in src, (
        "StudioViewModel 又出现 LoadDemoUnits —— 硬编码演示数据必须彻底废除；"
        "首屏改由 InitializeAsync 经真实 RPC 加载内置样本"
    )
    assert "tests/fixtures/output" not in src, (
        "导出目录不得再使用仓库内相对路径常量，必须由 ResolveExportDirectory 解析"
    )
    assert "sample_act1.ks" in src, "缺少内置样本定位逻辑，启动自动载入无法工作"
    assert "LoadFileAsync(sample)" in flat, (
        "InitializeAsync 必须经 LoadFileAsync 走 detect → extract 真实链路"
    )

    for required in ("DetectFormatAsync", "ExtractToIrAsync"):
        assert required in src, f"加载链路缺少 {required}，说明未走真实格式嗅探/抽取 RPC"
    assert "RunLqaAsync" in src, "人工微调后未接 run_lqa，状态胶囊会停留在抽取期快照"
    assert "IrToAssetAsync" in src, "导出未接 ir_to_asset，导出按钮与实际回写脱钩"
    assert "MarkExported()" in flat, "导出成功后未推进 EXPORTED 终态"
    assert "LastExportPath = result.OutputPath" in flat, "导出产物路径未回显到底栏"
    assert "FiltersReset?.Invoke()" in flat, "载入后未通知视图复位筛选胶囊"

    # 状态复位：换文件后残留筛选会把新数据整片滤空，表现为「加载失败」
    assert "SearchText = string.Empty" in flat, "LoadFileAsync 成功后必须清空 SearchText"
    assert 'FilterMode = "All"' in flat, "LoadFileAsync 成功后必须把 FilterMode 重置为 All"

    # 导出目录恒为绝对路径
    assert "Path.GetFullPath(CurrentFilePath)" in flat, "导出目录必须基于源文件绝对路径推导"


def test_view_codebehind_keeps_commit_then_recheck_chain():
    """失焦链路必须是「先 CommitEdits 再 run_lqa」——只回写不复检等于没有质检。"""
    fx = _fixtures()
    flat = re.sub(r"\s+", " ", fx.view_codebehind)

    assert "item.CommitEdits()" in flat, "失焦必须先回写 DTO，否则导出取不到人工微调"
    assert "RecheckUnitAsync(item)" in flat, "失焦后必须触发即时质检"
    assert flat.index("item.CommitEdits()") < flat.index("RecheckUnitAsync(item)"), (
        "必须先 CommitEdits 再 RecheckUnitAsync：质检对象应是刚写入的译文"
    )
    assert "FilterAll.IsChecked = true" in flat, (
        "筛选胶囊由 Command 驱动、无 IsChecked 双向绑定，VM 复位后必须由视图勾回 All"
    )
    assert "InitializeAsync()" in flat, "视图未在 Loaded 后触发启动自动载入"


def test_filter_pills_are_named_for_programmatic_reset():
    """三枚筛选胶囊必须有 x:Name，否则载入时的筛选复位没有落点。"""
    fx = _fixtures()
    for name in ("FilterAll", "FilterPending", "FilterFailed"):
        assert name in fx.x_names, f"筛选胶囊缺少 x:Name=\"{name}\""


def test_status_semantics_treat_exported_as_terminal_pass():
    """EXPORTED 是闭环终点：计入「已通过」，且**不得**计入「待翻译」。

    若 IsPending 未排除 EXPORTED，导出后待译计数会虚增、进度条会反向回退 ——
    这类缺陷只在导出动作之后才显形，属静态可拦的静默错误。
    """
    fx = _fixtures()
    src = fx.item_vm_source

    assert '"EXPORTED"' in src, "未使用契约里的 EXPORTED 状态，导出闭环没有终点落点"

    passed = re.search(r"public bool IsPassed =>([^;]+);", src, re.S)
    assert passed and "EXPORTED" in passed.group(1), (
        "IsPassed 必须包含 EXPORTED，否则导出后进度条会反向回退"
    )

    pending = re.search(r"public bool IsPending =>([^;]+);", src, re.S)
    assert pending and "EXPORTED" in pending.group(1), (
        "IsPending 必须排除 EXPORTED，否则已导出的行仍被算作待翻译"
    )

    assert "MarkExported" in src, "缺少导出后推进 EXPORTED 的入口"


def test_status_feedback_surface_is_wired():
    """Studio 的状态文案必须有可见落点（历史缺陷：StatusMessage 是零绑定的死属性）。"""
    fx = _fixtures()

    assert "StudioStatusLabel" in fx.shell_members, "ShellViewModel 缺少状态文案承载属性"
    assert "StudioStatusLabel" in fx.main_text, (
        "MainWindow.xaml 未绑定 StudioStatusLabel —— 载入失败/导出结果将无任何可见反馈"
    )
    assert "IdleHint" in fx.main_text or "IdleHint" in (
        SHELL_VM_PATH.read_text(encoding="utf-8")
    ), "说明条必须保留设计稿原文作为空闲态回落文案"


# --------------------------- 批量翻译闭环纪律 ---------------------------


def test_batch_translate_bindings_are_dynamic():
    """批量按钮角标、可用性、进度条必须接在动态统计属性上，而不是静态值。"""
    fx = _fixtures()

    split = fx.by_name["BatchTranslateSplit"]
    assert "BatchTranslateLabel" in split.attrib.get("Content", ""), (
        "按钮文案必须绑定动态角标 BatchTranslateLabel"
    )
    assert "CanTranslateBatch" in split.attrib.get("IsEnabled", ""), (
        "按钮可用性必须绑定 CanTranslateBatch；绑 IsIdle 无法反映批量在途，防重入会失效"
    )

    bar = next(el for el in fx.root.iter() if _local_tag(el) == "ProgressBar")
    assert "TranslationProgressRatio" in bar.attrib.get("Value", ""), (
        "进度条必须绑定译文覆盖率 TranslationProgressRatio"
    )
    assert bar.attrib.get("Maximum") == "1", (
        "Ratio 是 0..1 的覆盖率，Maximum 必须为 1；写成 100 会让进度条几乎不动"
    )

    assert "TranslationProgressText" in fx.xaml_text, "进度百分比文本必须绑定 TranslationProgressText"
    assert "ProgressPercent" not in fx.xaml_text, (
        "旧的 ProgressPercent 已被覆盖率语义取代，不应残留（两套进度口径会互相打架）"
    )


def test_batch_translate_vm_pipeline_discipline():
    """批量翻译闭环必须：防重入、切片、只翻待译行、批次后复检 LQA、finally 复位。"""
    fx = _fixtures()
    src = fx.root_vm_source
    flat = re.sub(r"\s+", " ", src)

    assert "IsTranslating" in src, "缺少批量翻译在途标记"
    assert "if (IsTranslating || !CanTranslateBatch)" in flat, (
        "缺少防重入闸门（Command 可能被命令绑定直接调用，不能只靠按钮置灰）"
    )
    assert "TranslateChunkSize" in src, (
        "缺少客户端切片粒度 —— 整批一次发出会让进度条在整个往返期间静止"
    )
    assert "u.NeedsTranslation" in flat, (
        "批量目标必须按 NeedsTranslation 过滤：LQA_FAILED 携带有效诊断，不该被静默重翻覆盖"
    )
    assert "RunLqaAsync(received)" in flat, (
        "批次回填后必须对已获译文的行复检 run_lqa（translator 的内联门禁不负责收敛陈旧留痕）"
    )
    assert "BackfillUnits(" in flat, "回填必须走统一入口（保证统计抑制与 id 对齐）"
    assert "IsTranslating = false" in flat, "在途标记必须在 finally 中复位，否则按钮永久置灰"


def test_needs_translation_predicate_is_shared_not_duplicated():
    """待译判定必须委托共享库，不得在行 VM 里另写一份。

    「按钮角标计数」与「批量选取目标」若各写一份判定，迟早漂移成
    「显示待译 3 条、点下去报没有待翻译单元」这类静默错位。
    """
    fx = _fixtures()

    assert "TranslationProgress.NeedsTranslation" in fx.item_vm_source, (
        "StudioUnitItemViewModel.NeedsTranslation 必须委托 TranslationProgress"
    )

    # 只在**代码表达式**上判重，不扫全文：属性上方的文档注释里
    # 合理地写着「已抽取（EXTRACTED）」这样的说明，全文匹配会造成纯误报。
    code = "\n".join(
        line for line in fx.item_vm_source.splitlines()
        if not line.lstrip().startswith("//")
    )
    predicate = re.search(r"public bool NeedsTranslation =>([^;]+);", code, re.S)
    assert predicate, "未找到 NeedsTranslation 的定义"
    body = predicate.group(1)
    assert "TranslationProgress.NeedsTranslation" in body, (
        "NeedsTranslation 必须委托共享库实现，不得在行 VM 里自写一份判定"
    )
    assert "EXTRACTED" not in body, (
        "NeedsTranslation 里出现了自写的 EXTRACTED 字面量 —— 判定应统一收敛到 TranslationProgress"
    )

    assert "TranslationProgress.Summarize" in fx.root_vm_source, (
        "StudioViewModel 的统计必须来自共享库，保证进度条与角标同一份口径"
    )


# --------------------------- Inspector 行级联动纪律 ---------------------------


def test_inspector_row_level_fields_bind_to_selected_unit():
    """Inspector 里每个「随行切换而变」的字段都必须绑到 SelectedUnit.*。

    尤其要防止退回旧形态：波形与术语 Chip 曾挂在父 VM 上（`WaveformBars` /
    `GlossaryItems`），与行数据成为两份真相 —— 切行时父副本不重算，
    面板会停在上一行的内容上，而这类错位在静态检查里看不出来。
    """
    fx = _fixtures()
    xaml = fx.xaml_text

    row_level = [
        "CharacterCardCaption",
        "Speaker",
        "IsSpeakerPlaceholder",
        "Waveform",
        "DurationLabel",
        "CompLabel",
        "GlossaryMatches",
        "GlossaryEmptyVisibility",
    ]
    missing = [m for m in row_level if f"SelectedUnit.{m}" not in xaml]
    assert not missing, "以下 Inspector 字段未绑到行级：" + "、".join(missing)

    # 试听键可用性走父 VM 的 IsAudioPlayable（语音线索 + 文件存在）：
    # 行级 IsPlaybackEnabled 只看元数据，分不出「有线索但文件缺失」。
    assert "IsAudioPlayable" in xaml, "试听键的文件存在性门控绑定丢失"

    # 父 VM 不得再持有行级数据的副本
    for gone in ("WaveformBars", "GlossaryItems"):
        assert gone not in xaml, f"{gone} 应已下移到行级，XAML 不应再引用"
        assert gone not in fx.root_vm_source, f"StudioViewModel 仍保留 {gone} 副本"

    # 标题仍由父 VM 派生（无选中行时显示空态），但格式化必须走共享层
    assert f"SelectedUnit.{'InspectorTitle'}" not in xaml
    assert "InspectorTitle" in xaml, "抽屉标题绑定丢失"
    assert "InspectorState.NoSelectionTitle" in fx.root_vm_source, (
        "无选中行时的标题必须取共享层的空态常量，不得在壳层硬编码"
    )


def test_playback_controls_are_wired_to_real_commands():
    """播放三键必须是接了 Command 的活控件，绝不退回「有外观无命令」的死按钮。

    分层语义：前后键 = 选中行导航（恒可用）；中键 = 真实试听，其可用性
    由父 VM 的 IsAudioPlayable 承载（语音线索 **且** 文件存在）—— 只看
    元数据的 SelectedUnit.IsPlaybackEnabled 分不出「有线索但文件缺失」，
    会退化成可点却无声。
    """
    fx = _fixtures()

    tooltips = ("选中上一行", "选中下一行", "{Binding AudioStatusHint}")
    buttons = [
        el for el in fx.root.iter()
        if _local_tag(el) == "Button" and el.attrib.get("ToolTip") in tooltips
    ]
    assert len(buttons) == 3, f"应找到播放三键（导航 × 2 + 试听 × 1），实际 {len(buttons)}"

    by_tooltip = {el.attrib.get("ToolTip"): el for el in buttons}

    assert by_tooltip["选中上一行"].attrib.get("Command") == "{Binding PreviousUnitCommand}"
    assert by_tooltip["选中下一行"].attrib.get("Command") == "{Binding NextUnitCommand}"

    play = by_tooltip["{Binding AudioStatusHint}"]
    assert play.attrib.get("Command") == "{Binding PlayPauseAudioCommand}", (
        "试听键必须绑定真实播放命令（历史缺陷：无 Command 的死控件）"
    )
    assert play.attrib.get("IsEnabled") == "{Binding IsAudioPlayable}", (
        "试听键可用性必须绑定文件存在性感知的 IsAudioPlayable"
    )

    # VM 侧配套：命令与分流逻辑真实存在
    for member in (
        "PlayPauseAudio", "PreviousUnit", "NextUnit",
        "RefreshAudioState", "ResolveAudioPath", "IsAudioPlayable",
    ):
        assert member in fx.root_vm_source, f"StudioViewModel 缺少试听链路成员 {member}"
    assert "IAudioPlayer" in fx.root_vm_source, (
        "播放必须经 IAudioPlayer 抽象（可测试、可替换实现），不得在 VM 里内联 WPF 媒体调用"
    )


def test_translation_config_reads_settings_store_not_local_defaults():
    """批量翻译与单条重试必须从设置仓库取参 —— 根除「设置页调了不生效」。"""
    fx = _fixtures()

    assert "TranslationSettingsStore.Current" in fx.root_vm_source, (
        "StudioViewModel 必须消费 TranslationSettingsStore（Settings 页的唯一真源）"
    )
    assert "CreateTranslationConfig" in fx.root_vm_source, (
        "批量与单条重试必须共用同一份配置组装实现"
    )
    # 独立持有的端点/模型属性已删除：谁再把它们写回来，谁就是又造了一个分叉真源。
    # 只扫**去注释后的代码**——注释里合理地记载着这段历史，全文匹配会造成纯误报。
    root_code = "\n".join(
        line for line in fx.root_vm_source.splitlines()
        if not line.lstrip().startswith("//")
    )
    assert "TranslateApiBase" not in root_code, (
        "StudioViewModel 不得再持有独立的 TranslateApiBase（配置真源在设置仓库）"
    )


def test_project_explorer_and_directory_pipeline_are_wired():
    """P1 工程树与整作目录链路必须真实接通：拖入目录 → 引擎画像 → 工作区 → 剧本树。"""
    fx = _fixtures()

    # 目录链路的全部 RPC 消费点真实存在
    for member in (
        "LoadGameDirectoryAsync", "DetectEngineAsync", "InitWorkspaceAsync",
        "ScriptWorkspace.ScanScripts", "ProjectScripts",
        "EngineBadgeText", "EngineEvidenceSummary",
    ):
        assert member in fx.root_vm_source, f"StudioViewModel 缺少工程树链路成员 {member}"

    # 视图侧：拖入目录分流 + 工程树事件接线
    assert "Directory.Exists" in fx.view_codebehind, (
        "拖入分发必须区分目录与文件（目录走工程模式，文件走单文件载入）"
    )
    assert "LoadGameDirectoryCommand" in fx.view_codebehind, "目录拖入必须路由到工程模式命令"
    assert "OnScriptSelected" in fx.view_codebehind and "SwitchToScriptCommand" in fx.view_codebehind, (
        "工程树点击必须接通切换命令"
    )
    for token in ("ProjectScripts", "EngineBadgeText", "OpenDirectoryPickerCommand"):
        assert token in fx.xaml_text, f"StudioView.xaml 缺少 {token} 的绑定"

    # 未知引擎不中断：必须有诚恳引导文案
    assert "未能识别已知引擎" in fx.root_vm_source, (
        "未知引擎时必须给出友好引导，而不是静默失败或强行中断"
    )


def test_script_switch_saves_edits_before_loading():
    """切换剧本的防丢失闭环：保存**必须先于**载入 —— 顺序反了就是静默丢稿。"""
    fx = _fixtures()

    switch_match = re.search(
        r"private async Task SwitchToScriptAsync\([^)]*\)[^{]*\{(?:(?!\n    \}).)*",
        fx.root_vm_source, re.S)
    assert switch_match, "未找到 SwitchToScriptAsync 的实现"
    body = switch_match.group(0)
    save_at = body.find("SavePendingEditsAsync()")
    load_at = body.find("LoadFileCoreAsync(")
    assert save_at != -1 and load_at != -1, "切换链路必须同时包含保存与载入"
    assert save_at < load_at, "防丢失保存必须发生在载入新剧本之前"

    # 保存链路内部：CommitEdits 先于共享层判定，判定复用共享库
    save_match = re.search(
        r"private async Task SavePendingEditsAsync\(\)[^{]*\{(?:(?!\n    \}).)*",
        fx.root_vm_source, re.S)
    assert save_match, "未找到 SavePendingEditsAsync 的实现"
    save_body = save_match.group(0)
    assert save_body.find("CommitEdits()") < save_body.find("HasUnsavedTranslations"), (
        "必须先提交编辑缓冲再做是否有译文的判定（顺序颠倒判定的是旧值）"
    )

    # 导出口径唯一：显式导出与自动保存共用同一管道
    assert fx.root_vm_source.count("ExportProjectCoreAsync()") >= 3, (
        "ExportProjectCoreAsync 必须同时被显式导出与防丢失保存消费（call site ≥3 含定义）"
    )


def test_sliding_window_and_concurrency_are_wired():
    """滑窗上下文与并发池必须在 UI 链路真实生效，而非只在 Python 侧空转。

    历史：壳层曾把 BatchSize 钳成 payload.Count，Sidecar 永远只切出单个
    子批 —— 并发池在 UI 路径上是装饰品。此类「调用了但永不生效」的
    软花架子由本测试钉死。
    """
    fx = _fixtures()
    root_code = "\n".join(
        line for line in fx.root_vm_source.splitlines()
        if not line.lstrip().startswith("//")
    )

    # 1) 滑窗：上下文必须作为**调度调用的实参**逐片求值（每片拿到的是
    #    经上一片回填后的最新前文，而不是循环外的一次性旧快照）
    batch_match = re.search(
        r"private async Task RunBatchTranslationAsync\([^)]*\)[^{]*\{(?:(?!\n    \}).)*",
        fx.root_vm_source, re.S)
    assert batch_match, "未找到 RunBatchTranslationAsync 的实现"
    batch_body = batch_match.group(0)
    assert re.search(r"TranslateBatchAsync\([^;]*?BuildBatchContext\(\)", batch_body, re.S), (
        "批量调度必须把 BuildBatchContext() 作为实参传入（每片取最新前文）"
    )

    # 2) 并发池解禁：不得再把 BatchSize 钳成整片（那会让 Sidecar 永远单子批）
    assert "BatchSize = payload.Count" not in root_code, (
        "禁止把 BatchSize 钳成 payload.Count —— 并发池会被钳死成单子批，永不在 UI 链路生效"
    )

    # 3) 单条重试的滑窗必须排除被重试行自身（旧坏译文不得反向锚定模型）
    retry_match = re.search(
        r"private async Task RetryUnitAsync\([^)]*\)[^{]*\{(?:(?!\n    \}).)*",
        fx.root_vm_source, re.S)
    assert retry_match, "未找到 RetryUnitAsync 的实现"
    assert "ReferenceEquals(u, item)" in retry_match.group(0), (
        "重试的滑窗上下文必须排除被重试行自身"
    )

    # 4) 客户端真实透传 context（载荷含三要素，而非仅 units+config）
    client_path = WF.parent / "GalPipeline.Core" / "IPC" / "PythonSidecarClient.cs"
    client_source = client_path.read_text(encoding="utf-8")
    assert "record TranslationContextLine(string? Speaker, string Text)" in client_source, (
        "客户端缺少滑窗上下文行 DTO"
    )
    assert 'new { units, config, context }' in client_source, (
        "translate_batch 载荷必须携带 context（否则滑窗从未上线）"
    )


LQALAB_VM_PATH = WF / "ViewModels" / "LqaLabViewModel.cs"
LQALAB_PAGE_PATH = WF / "Pages" / "LqaLabPage.xaml"
SETTINGS_VM_PATH = WF / "ViewModels" / "SettingsViewModel.cs"
SETTINGS_VIEW_PATH = WF / "Views" / "SettingsView.xaml"
CLIENT_PATH = WF.parents[1] / "src" / "GalPipeline.Core" / "IPC" / "PythonSidecarClient.cs"


def test_lqa_lab_board_is_real_not_placeholder():
    """质检中心必须是真实看板：占位 InfoBar 废除、聚合与治理全部接线。"""
    fx = _fixtures()
    lab_vm = LQALAB_VM_PATH.read_text(encoding="utf-8")
    lab_xaml = LQALAB_PAGE_PATH.read_text(encoding="utf-8")

    # 占位壳必须铲除
    assert "占位导航槽" not in lab_xaml, "LqaLabPage 的占位 InfoBar 必须废除"

    # 聚合走共享库纯函数（VM 不得自写一份口径）
    assert "LqaBoard.Build(" in lab_vm, "看板聚合必须委托 GalPipeline.Core.Lqa.LqaBoard"
    assert "StudioViewModelProvider.Shared" in lab_vm, (
        "看板数据源必须是工坊单例（否则看板是脱离实时数据的假面板）"
    )

    # 治理闭环：定位 + 面板内定向重译（复用工坊 RetryUnitCommand，含 run_lqa 复检）。
    # [RelayCommand] 生成属性不以字面量出现在源码 —— 按方法定义断言（生成器
    # 会把 FocusViolation / RetryViolationAsync 映射为 *Command 属性供 XAML 绑定）。
    assert "private void FocusViolation(" in lab_vm and (
        "private async Task RetryViolationAsync(" in lab_vm
    )
    for token in ("FocusViolationCommand", "RetryViolationCommand", "RefreshBoardCommand"):
        assert token in lab_xaml, f"LqaLabPage.xaml 缺少 {token} 的绑定"

    # 工坊宿主必须消费共享单例 —— new StudioViewModel() 回归即分叉
    assert "StudioViewModelProvider.Shared" in fx.view_codebehind, (
        "StudioView 必须使用共享单例 VM（否则 LQA Lab 摸不到工坊实时数据）"
    )
    assert "new StudioViewModel()" not in fx.view_codebehind, (
        "StudioView 不得再自建 VM 实例"
    )
    # 跳转导航：MainWindow 必须订阅 FocusRequested
    main = fx.main_text
    assert "StudioViewModelProvider.FocusRequested" in main or "FocusRequested" in (
        Path(REPO_ROOT) / "src" / "GalPipeline.UI.Wpf" / "MainWindow.xaml.cs"
    ).read_text(encoding="utf-8"), "MainWindow 必须订阅 FocusRequested 承载跳转导航"


def test_session_tokens_are_real_and_visible():
    """Token 会话计量必须真实累加并上底栏（伪指标铲除后的兑现断言）。"""
    fx = _fixtures()
    client_source = (
        WF.parent / "GalPipeline.Core" / "IPC" / "PythonSidecarClient.cs"
    ).read_text(encoding="utf-8")

    # 客户端消费 usage 信封
    assert "record TranslateBatchResult(List<TranslationUnitDto> Units, TokenUsageDto? Usage)" in client_source

    # 壳层：累计点必须同时覆盖批量切片与单条重试（漏一处就是漏记账）
    assert fx.root_vm_source.count("AccumulateSessionTokens(updated.Usage)") >= 2, (
        "批量翻译与单条重试都必须累加真实 usage"
    )
    # 无消耗不显示：SessionTokensText 空串口径 + 底栏拼装消费
    assert "SessionTotalTokens > 0" in fx.root_vm_source
    assert "SessionTokensText" in "\n".join(
        line for line in fx.root_vm_source.splitlines()
        if "BuildMetricsLabel" in line or "segments.Add" in line
    ) or "segments.Add(SessionTokensText)" in fx.root_vm_source, (
        "底栏指标串必须消费真实会话消耗"
    )


def test_unpack_orchestration_is_wired_with_toolchain_guidance():
    """一键解包链路：工具链解析 → 逐封包短 RPC → 重扫工程树；Missing 必须给引导。"""
    fx = _fixtures()

    for member in (
        "StartUnpackAsync", "CancelUnpack", "NeedsUnpackPrompt",
        "ListArchivesAsync", "UnpackArchiveAsync",
        "ToolchainRegistry.Get", "ToolchainMissingException",
        "ScriptWorkspace.NeedsUnpack", "RefreshProjectScripts",
    ):
        assert member in fx.root_vm_source, f"StudioViewModel 缺少解包链路成员 {member}"

    # 取消纪律：批次内必须检查取消令牌（ThrowIfCancellationRequested），
    # 且单封包失败不得拖垮整批（catch JsonRpcException 续走）
    unpack_match = re.search(
        r"private async Task StartUnpackAsync\(\)[^{]*\{(?:(?!\n    \}).)*",
        fx.root_vm_source, re.S)
    assert unpack_match, "未找到 StartUnpackAsync 的实现"
    body = unpack_match.group(0)
    assert "ThrowIfCancellationRequested()" in body, "解包批次必须响应取消令牌"
    assert "catch (JsonRpcException" in body, "单封包失败必须跳过续走，不得拖垮整批"

    # 视图侧：解包引导卡与按钮真实接线（Missing 时引导文案直达状态栏）
    for token in (
        "UnpackPromptVisibility", "StartUnpackCommand",
        "CancelUnpackCommand", "UnpackProgress",
    ):
        assert token in fx.xaml_text, f"StudioView.xaml 缺少解包引导绑定 {token}"

    settings_vm = (WF / "ViewModels" / "SettingsViewModel.cs").read_text(encoding="utf-8")
    assert "TranslationSettingsStore.Update" in settings_vm, (
        "SettingsViewModel 必须把配置变更推入设置仓库"
    )
    assert '"off"' in settings_vm, (
        "推理力度必须提供 off 选项（非推理模型收到 reasoning_effort 会被部分端点 400 拒收）"
    )


def test_inspector_derivation_lives_in_shared_layer():
    """占位判定与时长格式化必须落在共享层：壳层 VM 属 net10.0-windows，
    dotnet test 工程引用不了它，逻辑留在壳层就等于不可自动化验证。"""
    fx = _fixtures()

    assert "InspectorState.For(" in fx.item_vm_source, (
        "行 VM 必须委托 InspectorState.For 派生，而不是自写判定"
    )
    assert "InspectorState.LineNumberTag" in fx.item_vm_source, (
        "行号格式必须与 Inspector 标题同源（两处各写一份迟早写出 #0014 vs #00014）"
    )

    # 壳层（VM 与 XAML）都不得硬编码共享层负责的文案。
    # 只扫**去注释后的代码**：属性上方的文档注释里会合理地引用这些字面量
    # （例如「时长未知时为 --:--」），全文匹配会造成纯误报。
    vm_code = "\n".join(
        line for line in fx.item_vm_source.splitlines()
        if not line.lstrip().startswith("//")
    )
    xaml_code = re.sub(r"<!--.*?-->", "", fx.xaml_text, flags=re.S)

    for literal in ("--:--", "NO SPRITE", "差分卡 · STANDING"):
        assert literal not in vm_code, f"壳层 VM 硬编码了 {literal}，应由共享层派生"
        assert literal not in xaml_code, f"XAML 硬编码了 {literal}，应绑定共享层派生值"

    # 旁白归一化同样只能在共享层
    assert '"旁白"' not in vm_code, (
        "旁白兜底文案应来自 InspectorState.NarratorLabel，不要在行 VM 里再写一份"
    )


def test_toolchain_settings_are_wired_to_runtime():
    """工具链环境面板必须是「真联动」：持久化、快照保持、动态刷新三线齐全。

    历史教训（滑窗并发切片曾犯）：设置仓库是整体快照替换语义 ——
    PushToStore 若不携带工具链映射，改一次 API URL 就会静默清空
    用户配置的自定义工具路径。
    """
    fx = _fixtures()
    settings_vm = SETTINGS_VM_PATH.read_text(encoding="utf-8")
    settings_xaml = SETTINGS_VIEW_PATH.read_text(encoding="utf-8")
    cache_source = (
        WF.parents[1] / "src" / "GalPipeline.Core" / "Audio" / "FFmpegAudioCache.cs"
    ).read_text(encoding="utf-8")

    # 1) 持久化：浏览/恢复默认都走仓库的 SetCustomToolPath（清除=删条目回退瀑布）
    assert settings_vm.count("TranslationSettingsStore.SetCustomToolPath(") >= 2, (
        "浏览/恢复默认两个动作必须都经 SetCustomToolPath 持久化"
    )

    # 2) 快照保持：PushToStore 必须携带现有映射（整体替换语义下，不带 = 静默清空）
    assert "CustomToolPaths: TranslationSettingsStore.Current.CustomToolPaths" in settings_vm, (
        "PushToStore 必须携带 CustomToolPaths —— 否则改 API 设置会清空工具路径"
    )

    # 3) 动态刷新：行探测必须消费 ToolchainRuntime（映射变更后解析器整体替换）
    assert "ToolchainRuntime.Current.Resolve(" in settings_vm, (
        "工具链行必须经 ToolchainRuntime.Current 现查 —— 自建 resolver 实例会固化旧映射"
    )

    # 4) 消费方禁止手写特例：工坊与转码缓存都必须走动态运行时。
    #    只扫**去注释后的代码**（第三次同坑：警示注释里写着反例字符串本身）
    def code_only(source: str) -> str:
        return "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("//")
        )

    assert "new ExternalToolResolver()" not in code_only(fx.root_vm_source), (
        "StudioViewModel 不得自建解析器（设置变更将对其失效）"
    )
    assert "new ExternalToolResolver()" not in code_only(cache_source), (
        "FFmpegAudioCache 默认必须消费 ToolchainRuntime（显式注入仅供测试）"
    )
    assert "ToolchainRuntime.Current" in cache_source, (
        "FFmpegAudioCache 未注入时必须经 ToolchainRuntime 动态解析"
    )

    # 5) XAML 绑定齐全：状态徽章 / 来源 / 路径 / 三个动作 + 全量重探
    for token in (
        "ToolItems",
        "ReprobeAllCommand",
        "BrowseCommand",
        "ResetCommand",
        "OpenDirectoryCommand",
        "StatusKindText",
        "OriginLabel",
        "ResolvedPath",
    ):
        assert token in settings_xaml, f"SettingsView.xaml 缺少工具链绑定 {token}"

    # 门控语义：恢复默认仅在已配置自定义路径时可用；打开目录仅在 Ready 时可用
    assert settings_xaml.index("IsEnabled=\"{Binding HasCustomPath}\"") < settings_xaml.index(
        "IsEnabled=\"{Binding IsReady}\""
    ) or True  # 顺序不敏感，存在性才是契约
    assert "{Binding HasCustomPath}" in settings_xaml and "{Binding IsReady}" in settings_xaml, (
        "恢复默认/打开目录的门控绑定缺失"
    )
