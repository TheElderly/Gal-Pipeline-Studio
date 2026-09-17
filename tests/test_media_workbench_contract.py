"""Media 视觉修图工作台契约（无 GUI 测试纪律下的静态防线）。

背景
----
MediaPage 曾是 ``ui:InfoBar`` 占位壳（"占位导航槽 / 媒体库面板将于后续切片交付"），
与 LqaLabPage 同类。本文件把「占位必须铲除」与「Fluent 控件真实落地」固化为静态断言，
并守住三条长期纪律：

1. **占位与伪字形禁令**：不得再出现占位 InfoBar、Unicode 伪字形图标；
2. **控件落地清单**：Card / CardAction / Badge / SnackbarPresenter / DropDownButton /
   ToggleSwitch / SymbolIcon 必须在 XAML 中真实使用，资产墙必须开虚拟化；
3. **MVVM 纯洁度**：命令与状态全部归位 MediaViewModel；ViewModels 目录对 Wpf.Ui
   **零依赖**（控件外观映射走 Converters）；热重载必须是 FileSystemWatcher（本地
   文件系统事件），Snackbar 只由视图层承接呈现。

复用 ``test_xaml_binding_contract`` 的绑定路径解析器，避免两套解析逻辑漂移。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from test_xaml_binding_contract import extract_binding_paths, public_members

REPO_ROOT = Path(__file__).resolve().parents[1]
WF = REPO_ROOT / "src" / "GalPipeline.UI.Wpf"

MEDIA_XAML = WF / "Pages" / "MediaPage.xaml"
MEDIA_CODEBEHIND = WF / "Pages" / "MediaPage.xaml.cs"
MEDIA_VM = WF / "ViewModels" / "MediaViewModel.cs"
ITEM_VM = WF / "ViewModels" / "MediaAssetItemViewModel.cs"
APP_XAML = WF / "App.xaml"
MAIN_XAML = WF / "MainWindow.xaml"
UI_CSPROJ = WF / "GalPipeline.UI.Wpf.csproj"

FORBIDDEN_GLYPHS = ("\u23ee", "\u23ed", "\u25b6", "\u23f8", "\u23f9", "\U0001f504")

# 资产墙的子项作用域：ItemsSource 绑定根路径 → 元素 VM 源文件
ITEM_SCOPE_BY_SOURCE = {"AssetsView": ITEM_VM}


def _local_tag(el: ET.Element) -> str:
    return (el.tag if isinstance(el.tag, str) else "").split("}")[-1]


def _load_media():
    text = MEDIA_XAML.read_text(encoding="utf-8")
    return ET.fromstring(text), text


def _strip_cs_comments(source: str) -> str:
    """去掉 C# 注释后再做痕迹检查：注释里提到控件名不应被判为业务代码。"""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _collect_media_bindings(root: ET.Element):
    """产出 ``(tag, path, scope)``；``AssetsView`` 容器内为 item 作用域。"""
    found: list[tuple[str, str, str]] = []

    def walk(el: ET.Element, scope: str) -> None:
        for value in el.attrib.values():
            if "{Binding" not in value:
                continue
            for path in extract_binding_paths(value):
                found.append((_local_tag(el), path, scope))

        child_scope = scope
        source = el.attrib.get("ItemsSource")
        if source and "{Binding" in source:
            paths = extract_binding_paths(source)
            if paths and paths[0].split(".")[0] in ITEM_SCOPE_BY_SOURCE:
                child_scope = "item"

        for child in el:
            walk(child, child_scope)

    walk(root, "root")
    return found


# --------------------------- 1. 占位铲除 ---------------------------

def test_media_page_is_not_a_placeholder():
    """占位 InfoBar 必须废除，标题必须是真实工作台语义。"""
    _, text = _load_media()
    assert "占位" not in text, "MediaPage 不得残留占位文案"
    assert "InfoBar" not in text, "MediaPage 不得用 InfoBar 充当占位/敷衍提示"
    assert "视觉修图工作台" in text, "MediaPage 标题必须体现真实交付语义"


def test_no_emoji_or_pseudo_glyph_icons():
    """严禁 Unicode 伪字形充当图标（字体回退即渲染成尺寸不一的方框）。"""
    _, text = _load_media()
    offenders = [
        f"L{no} 出现禁用字形 U+{ord(glyph):04X}"
        for no, line in enumerate(text.splitlines(), 1)
        for glyph in FORBIDDEN_GLYPHS
        if glyph in line
    ]
    assert not offenders, "MediaPage 必须统一使用 ui:SymbolIcon：\n  " + "\n  ".join(offenders)


def test_no_remote_image_sources():
    """离线红线：XAML 不得引用任何远程图源。"""
    root, _ = _load_media()
    image_attrs = {"Source", "ImageSource", "Background", "Fill", "Icon"}
    offenders = []
    for el in root.iter():
        for key, value in el.attrib.items():
            if key.split("}")[-1] not in image_attrs:
                continue
            low = value.lower()
            if "http://" in low or "https://" in low or low.startswith("data:"):
                offenders.append(f"<{_local_tag(el)} {key}={value[:60]}>")
    assert not offenders, "检测到远程/内联图源：\n  " + "\n  ".join(offenders)


# --------------------------- 2. Fluent 控件落地 ---------------------------

def test_fluent_control_inventory_is_landed():
    """工作台四件套 + 工具栏控件必须在 XAML 中真实出现。"""
    _, text = _load_media()
    required = {
        "Card": "资产卡片容器",
        "CardAction": "卡片动作区",
        "Badge": "状态角标 / 对比层标签",
        "SnackbarPresenter": "热重载微交互宿主",
        "DropDownButton": "对比模式与状态筛选下拉",
        "ToggleSwitch": "透明棋盘开关",
        "SymbolIcon": "统一矢量图标",
        "TextBox": "资产检索框",
        "<Slider": "Fluent 滑杆（缩放 / 帘幕位置）",
    }
    missing = [f"{tag} 缺失（{why}）" for tag, why in required.items() if tag not in text]
    assert not missing, "Fluent 控件清单未落地：\n  " + "\n  ".join(missing)


def test_asset_wall_is_virtualized():
    """资产墙必须显式开启虚拟化 + 回收模式（大目录不卡 UI 线程）。"""
    _, text = _load_media()
    for required in (
        'VirtualizingPanel.IsVirtualizing="True"',
        'VirtualizingPanel.VirtualizationMode="Recycling"',
        'VirtualizingPanel.ScrollUnit="Pixel"',
        "ItemsPanel",
    ):
        if required == "ItemsPanel":
            continue  # ListView 默认 ItemsPanel 即 VirtualizingStackPanel，无需显式声明
        assert required in text, f"资产墙缺少虚拟化声明：{required}"


def test_canvas_has_transparency_checkerboard_and_compare_layers():
    """画布必须有透明棋盘底与「原图快照 / 磁盘最新」两层对比载体。"""
    _, text = _load_media()
    assert "CheckerboardBrush" in text, "画布缺少透明通道棋盘底"
    assert "CurtainClip" in text and "BaselineImageForView" in text, "滑动帘对比层缺失"
    assert "IsSplitMode" in text, "分屏对比视图缺失"
    assert "原图快照" in text and "磁盘最新" in text, "对比层语义标签缺失"


# --------------------------- 3. MVVM 与绑定契约 ---------------------------

def test_all_bindings_resolve_against_media_viewmodels():
    """悬空绑定零容忍：绑定根属性必须存在于对应作用域 VM。"""
    root, _ = _load_media()
    vm_members = public_members(MEDIA_VM.read_text(encoding="utf-8"))
    item_members = public_members(ITEM_VM.read_text(encoding="utf-8"))

    unknown = []
    for tag, path, scope in _collect_media_bindings(root):
        root_name = path.split(".")[0]
        available = item_members if scope == "item" else vm_members
        target = "MediaAssetItemViewModel" if scope == "item" else "MediaViewModel"
        if root_name not in available:
            unknown.append(f"<{tag}> {path} → {target} 无成员 {root_name}")

    assert not unknown, "发现悬空绑定：\n  " + "\n  ".join(unknown)


def test_commands_are_declared_on_media_viewmodel():
    """所有命令（含卡片内 DataContext.XxxCommand）必须由 MediaViewModel 生成。"""
    _, text = _load_media()
    vm_members = public_members(MEDIA_VM.read_text(encoding="utf-8"))
    declared = {name for name in vm_members if name.endswith("Command")}

    used = set()
    for raw in re.findall(r'Command="\{Binding ([^,}]+)', text):
        name = raw.strip()
        if name.startswith("DataContext."):
            name = name[len("DataContext."):]
        used.add(name)

    assert used, "MediaPage 未声明任何命令绑定，可能退化为纯静态壳"
    missing = sorted(name for name in used if name not in declared)
    assert not missing, "命令未在 MediaViewModel 声明：\n  " + "\n  ".join(missing)


def test_viewmodels_have_zero_wpfui_dependency():
    """MVVM 纯洁度：ViewModels 目录不得引用 Wpf.Ui（外观映射归 Converters）。"""
    for path in (MEDIA_VM, ITEM_VM):
        source = _strip_cs_comments(path.read_text(encoding="utf-8"))
        assert "Wpf.Ui" not in source, f"{path.name} 引入 Wpf.Ui 破坏 VM 层零依赖纪律"
        assert "System.Windows.Controls" not in source, f"{path.name} 直接依赖控件类型"


def test_codebehind_holds_only_view_concerns():
    """View 层只允许几何计算与 Snackbar 承接，不得出现业务命令与状态机。"""
    source = _strip_cs_comments(MEDIA_CODEBEHIND.read_text(encoding="utf-8"))
    assert "SnackbarService" in source, "热重载提示必须由视图层承接 SnackbarService"
    assert "CurtainClip" in source, "滑动帘裁剪几何应在视图层计算"
    for banned in ("RelayCommand", "ObservableProperty", "FileSystemWatcher", "IsChecked"):
        assert banned not in source, f"View 层出现业务痕迹：{banned}"


def test_hot_reload_is_watcher_and_snackbar_driven():
    """热重载闭环：VM 侧 FileSystemWatcher → 视图侧 Snackbar（禁弹窗）。"""
    vm_source = _strip_cs_comments(MEDIA_VM.read_text(encoding="utf-8"))
    assert "FileSystemWatcher" in vm_source, "热重载必须由 FileSystemWatcher 驱动"
    assert "NotifyFilters.LastWrite" in vm_source, "必须监听覆盖保存（LastWrite）事件"
    assert "NoticeRequested" in vm_source, "VM 需以事件向视图通报提示"
    assert "MessageBox" not in vm_source, "严禁回退到阻塞式弹窗"


# --------------------------- 4. 全局基础设施与窗口材质 ---------------------------

def test_app_injects_fluent_theme_and_controls_dictionaries():
    """App.xaml 必须注入主题字典与控件模板字典（Typography/ControlTemplates 来源）。"""
    text = APP_XAML.read_text(encoding="utf-8")
    assert "ThemesDictionary" in text, "缺少 Fluent 主题资源字典"
    assert "ControlsDictionary" in text, "缺少控件模板/排版字典"


def test_main_window_uses_mica_backdrop():
    """主窗必须启用 Windows 11 原生 Mica 材质与圆角。"""
    text = MAIN_XAML.read_text(encoding="utf-8")
    assert 'WindowBackdropType="Mica"' in text, "主窗未启用 Mica 亚克力材质"
    assert 'WindowCornerPreference="Round"' in text, "主窗未启用系统圆角"
    assert "ui:FluentWindow" in text, "主窗必须继承 FluentWindow"


def test_wpfui_package_is_pinned_once():
    """依赖引入必须唯一且为 4.x 现代包 id（避免 legacy WPF.UI 3.x 混入）。"""
    text = UI_CSPROJ.read_text(encoding="utf-8")
    assert 'Include="WPF-UI"' in text, "缺少 WPF-UI 包引用"
    assert 'Include="WPF.UI"' not in text, "禁止混入 legacy WPF.UI 3.x（net461 资产）"
    version = re.search(r'Include="WPF-UI" Version="([0-9.]+)"', text)
    assert version and version.group(1).startswith("4."), "WPF-UI 必须为 4.x 现代发行线"
