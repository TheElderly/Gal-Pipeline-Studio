# GalPipeline 阶段交接与后续开发指南

> 本文档由上一任 AI（Agent 0 协调 + 桌面端负责人）在额度耗尽前撰写。
> 目标读者：任何接手本仓库的 AI（Cursor / Codex / Web Agent 等）。
> 请先完整读完本文档，再动第一行代码。

## 1. 项目定位与业务核心

- **产品定位**：工业级 Galgame（视觉小说）本地化基础设施与计算机辅助翻译 (CAT) 平台。
- **核心价值**：通过 **Gal-IR（中间表示）** 隔离引擎控制宏（如 `<wait:300ms>`、跳转指令、立绘差分），提供
  **双栏审校 + 批处理 LLM 翻译 + 实时 LQA 门禁规则 + 原作无损回写编译** 的完整闭环。
- **数据流**：`.ks` 剧本 → `detect` 格式嗅探（魔数/特征，禁后缀判断）→ `extract_to_ir` 抽取为
  GalIRProject（原文/译文/宏标记 AtomicTag 三元组登记）→ `translate_batch`（OpenAI 兼容端点批量调度，
  支持 `reasoning_effort` 推理力度）→ LQA 即时门禁（引号配平/破折号成双/锚点守恒）→
  **人工内联审校 → `run_lqa` 复核** → `ir_to_asset` 原位回写（骨架行逐字保真 + 宏逐字节复写 +
  UTF-8 BOM）→ 壳层把单元推进 `EXPORTED` 终态。

## 2. 系统双进程架构

- **前端宿主（C# WPF，.NET 10，lepoco/wpfui v4.3 Fluent 2）**：
  - `src/GalPipeline.Core`：`PythonSidecarClient`（StdIO JSON-RPC 2.0 异步客户端，
    TaskCompletionSource 按 id 匹配并发响应）+ Gal-IR 强类型 DTO 信封。
  - `src/GalPipeline.UI.Wpf`：`FluentWindow` 主壳 + NavigationView 四槽
    （Studio 剧本工坊 / Asset Hub / LQA Lab / Settings），底部全局状态栏
    （Sidecar 指示灯 / 文件行数 / Session Tokens / Latency，经 `StudioViewModel.MetricsSink` 汇入）。
  - `src/GalPipeline.UI`（WinUI 3 版界面，与 UI.Wpf 并存的另一宿主形态）。
- **后端核心（Python Sidecar，`python -X utf8 -m core.server.rpc`）**：
  - 行式 JSON-RPC 2.0 守护进程（`core/server/rpc.py`），已注册方法：
    `ping` / `fetch_models` / `detect_format` / `extract_to_ir` / `translate_batch` /
    `ir_to_asset` / `run_lqa` / `match_glossary`。
  - 适配器层 `core/adapters/`：`KagAdapter`（KAG .ks 剧本）+ `image.py`（RGBA PNG-32 离线归一化）。
    引擎选择走模块级 `_ADAPTER_FACTORIES` 注册表，新增引擎只改这张表。
  - 翻译调度 `core/pipeline/translator.py`：OpenAI 兼容批次协议（编号行 `N. 【角色】正文`）、
    `reasoning_effort` 注入、清洗纪律（**严禁触碰「」引号**——那是 LQA 的断言靶子）。
  - LQA 门禁 `core/lqa/rules.py`：引号栈扫描配平 / 破折号成双游程 / 宏锚点守恒，
    error 级阻断、warning 级放行留痕（`metadata["lqa_issues"]`）。

## 2.5 端到端架构拓扑与工具链在线状态（2026-09-17 E2E 穿透后核对）

```
游戏原版封包/资产
      │  ① 解包 / 提取（真机 CLI，engine/bridge 调度）
      ▼
  core/engine/bridge.py ── RelayRecipe（core/adapters/relay/recipes.py）
      │        dump_tool → 中间文本（relay.json / relay.tsv / relay.marked）
      ▼
  core/adapters/**（Gal-IR 抽取）→ 译文注入
      │  ② LQA 门禁（core/lqa/rules.py：控制符守恒 / 标记配平 / 标点）
      ▼
  外科回写（ir_to_asset：仅改文本值，非文本节点逐字节不动）
      │  ③ 真机回编译（recompile_tool）
      ▼
  localized/ 资产树（**只放最终资产**；中间件必须落 work/）
      │  ④ Dirty-Only 收集 + 中间产物机拦（core/packaging/collector.py）
      ▼
  core/packaging/pipeline.py ── 策略路由
      ├─ OverlayPatch：暂存 {artifact}.d + 可选 pack 容器
      ├─ LooseDirectory：解压即用覆盖目录
      └─ XDeltaDiff：xdelta3 生成差分 + **当场还原验证**（-d 回放哈希一致才交付）
      ▼
  dist/{game_id}_patch/（载荷 + manifest.json + INSTALL.txt）
      │  ⑤ core/launch/launch_config.py（LaunchConfig）
      ▼
  启动引导：LEProc -run/-runas 免转区；未装 LE → 直启降级 + 诊断披露
```

**8 个真实 CLI 在线状态（`scripts/audit_toolchain_dependencies.py` → 8/8 Found）**

| CLI | 归属 | 装载位置 | 用途 |
|---|---|---|---|
| `PsbDecompile.exe` | FreeMote v4.7.0 | `tools/freemote/` | PSB → JSON 真机 dump |
| `PsBuild.exe` | FreeMote v4.7.0 | `tools/freemote/` | JSON → PSB 真机回编译 |
| `ffmpeg.exe` | FFmpeg N-126593 | `tools/ffmpeg/` | 多媒体唯一底层（转码/提取） |
| `xdelta3.exe` | Xdelta 3.2.0 | `tools/xdelta/` | 增量差分生成 / 还原 |
| `GARbro.Console.exe` | GARbro-Mod（源码单步编译） | `tools/garbro/` | 通用封包解包（只读兜底） |
| `BGI_Hazuki_TextTool.exe` | BGI Hazuki v1.1.4 | `tools/bgi/` | BGI DSC 脚本 extract/apply |
| `cs2_decompile.exe` | CatSystem2 STT | `tools/cs2/` | CST/FES/ANM 反编译 |
| `mc.exe` | CatSystem2 官方 | `tools/cs2/` | Message Compiler（回编译方向待样本 E2E） |

配套辅助（非中继主链）：`tools/majiro/`（5）、`tools/qlie/`（2）、`tools/circus/`（4）、
`tools/vntextpatch/`（VNTextPatch 多引擎通用）、`tools/krkr_dump/`（KrkrDump 运行时倾倒）、
`tools/locale_emulator/`（LEProc 免转区）。

**tools/ 目录纪律**：`tools/` 根**只允许目录**（12 个生产工具目录 + `_archive/`）；
源包、参考源码、被取代的旧版本一律进 `_archive/`（含 `chinesize/` 全量隔离扫描、
`wpfui-4.3.0-src`、`xdelta3-3.1.0-superseded`）。`tools/` 已在 `.gitignore` 内。


## 3. 核心纪律与避坑经验（血泪教训）

- **严禁前台 GUI 测试**：严禁启动任何前台窗口、屏幕截图、坐标点击或 UIA 自动化脚本，
  一律使用 `dotnet build --no-incremental`、`dotnet test` 与 `pytest tests/` 纯命令行验证。
- **XAML 静态资源陷阱**：严禁在样式中直接引用未在资源字典中落盘的 `StaticResource`
  （此前引发 `XamlParseException` 启动闪退，现已在 `App.xaml.cs` 配置黑匣子日志
  `gal-pipeline-crash.log`：Dispatcher / AppDomain / UnobservedTask 三路捕获全链落盘）。
- **持久化契约**：必须保留 `ui:TextBox` 的 `LostFocus -> CommitEdits()` 链路，
  防止列表虚拟化滚动时丢字；导出前必须 `foreach (Units) CommitEdits()` 全量同步，
  并**从行 VM 的最新 Model 重建项目信封**（抽取期快照不含译文与人工微调——曾致全量译文静默丢失）。
- **单一底栏**：主外壳 StatusBar 保持单一，由 `StudioViewModel.MetricsSink` 动态汇入指标，
  严禁在子视图中重复实现底栏。
- **分角色智能体注册路径**：工作区专属分角色定义在 `.codebuddy/agents/`（**不是** `.workbuddy/agents/`，
  后者只是桌面端配置目录）。frontmatter 必填 `name`（须等于文件名）+ `description`，
  其余可用 `tools` / `model` / `effort` / `permissionMode` / `skills` 等。
  手动新增的文件**需重启会话**才进注册表；`.zcode/agents/` 仅保留为同内容的镜像副本。
- **WPF-UI 4.3 专属坑**：
  - `SelectionChanged` / `ItemInvoked` 事件链在本宿主形态下不触发——导航统一走
    `OnPaneMouseUp` + `VisualTreeHelper.HitTest` 命中测试（`MainWindow.xaml.cs`）；
  - 包内不存在 WinUI 生态的 `SettingsCard`，用 `CardControl` / `CardExpander`；
  - `StackPanel` 无 `Spacing`；数据模板内严禁 `x:Bind`（WPF 侧用经典 `{Binding}` + INPC）。
- **C# 保留字陷阱**：`checked` / `fixed` 不能用作变量名（`var checked = ...` 会炸出一串
  CS1002/CS1003 语法错误，报错行号还指在别处）。测试里写闸门对象时改用 `gated` / `repairedUnits`。
- **导出产物与运行时日志**：汉化剧本落在源文件同级 `localized/`（含 `tests/fixtures/localized/`），
  App 黑匣子日志为工作区根的 `gal-pipeline-crash.log`；二者均已在 `.gitignore`，
  不是交付物，不要提交。
- **公式类逻辑一律下沉到 `src/GalPipeline.Core`**：壳层 VM 属 `net10.0-windows`，
  `dotnet test` 工程（`net10.0`，无 UseWPF）引用不了它。凡是「会被算错」的东西
  （进度统计、衰减、判定谓词、Inspector 派生）都放共享库，测试才够得着。
- **静态断言必须扫「去注释后的代码」，不能扫全文**：这条已踩两次 ——
  `assert "EXTRACTED" not in vm_source` 被属性上方的文档注释（「已抽取（EXTRACTED）」）误伤，
  `assert "--:--" not in vm_source` 同理。写法：先剥掉 `//` 行与 `<!-- -->` 块，
  再用正则捕出具体**代码表达式**（如 `public bool X =>([^;]+);`）后判重。

## 4. 当前基线与待办任务

- **分支**：`feature/fluent-wpfui-architecture`（main 已同步至 `v0.7.0` 里程碑）。
- **构建状态**：`dotnet build --no-incremental` 0 警告 0 错误；测试全绿（C# **139/139**，Python **453**）。
  （2026-09-16 复核：Python 159 → 164 → 166 → 171 → 174 → 200 → 213 → **239**；
  绑定契约测试 5 → 12 → 14 → 20 → 23 → **26** 条；C# 用例 8 → 15 → 47 → **77** 条。）
- **双进程验证命令**：
  - `python -m pytest tests/`（Python 全量回归）
  - `dotnet test`（C# IPC 进程通信）
  - `dotnet run --project src/GalPipeline.UI.Wpf`（拉起桌面端 + 自动拉起 Sidecar）
- **本地离线演练资产**：`tests/fixtures/sample_act1.ks`（真实 KAG 样本）+
  `tests/fixtures/mock_llm_server.py`（127.0.0.1:18080 环回 Mock LLM，零额度）。

### StudioView 视觉微调（✅ 2026-09-16 已完成）

1. Col 3 译文输入框聚焦亮蓝底线 —— 用 2 行 Grid **自绘叠加层**（`TranslationUnderlineIdle` /
   `TranslationUnderlineFocus`），`DataTemplate.Triggers` 监听 `IsKeyboardFocusWithin` 做
   120ms 淡入 / 180ms 淡出，并挂 8px 蓝色 `DropShadowEffect` 发光。
   **设计纪律：静默态两条线都必须 `Opacity="0"`——译文列未编辑时是纯净文本，绝不出现常驻横线**；
   悬停反馈交给行背景，**不要**再给底线加 hover 触发器（该纪律已固化为
   `tests/test_xaml_binding_contract.py` 的断言）。
   **严禁在 TextBox 上回填局部 `BorderBrush`**：WPF 优先级为「局部值 > 样式触发器」，
   局部配色会压垮焦点视觉（这正是本项的历史根因）。
2. Col 4 状态胶囊 1px 半透明 Keyline —— `StudioUnitItemViewModel.StatusKeylineBrush`（35% alpha）。
3. Col 1 角色徽章格式化为 `[Speaker]` —— `SpeakerBadge`；
   **`Speaker` 原始属性不可改**（搜索过滤 `FilterUnit` + 立绘占位均复用）。
4. Inspector 标题单行化 —— 删除重复的 `SelectedUnit.LineNumberTag` 文本块；
   同时修复悬空绑定 `Visibility="{Binding InspectorVisibility}"`（VM 从未定义该属性，
   绑定静默失败导致折叠命令无效果）→ 改绑 `InspectorContentVisibility`，
   接通 `Width="{Binding InspectorWidth}"`，并补上 32px 折叠停靠把手（`ChevronLeft24`）。
5. 原文列 / 译文列基线对齐（对照设计图精修）—— 两列容器统一 `VerticalAlignment="Top"` +
   6px 顶部留白（`WrapPanel` 无 `Padding` 属性，用等效 `Margin="0,6,10,6"` 承载），
   保证带 `<wait:300ms>` 宏标签的高行与单行译文首行基线平齐，消除上下悬空错位。
6. Inspector 立绘区重塑 —— 移除灰色 `Person48` 系统图标，改为 150×200（**3:4**）差分卡：
   `BorderBrush="#252E42"` + `CornerRadius="8"` 底框 + 微弱十字校准网格 +
   `Rectangle` 虚线内描边（`StrokeDashArray="3 3"`，注意 `Border` 本身不支持虚线）
   + 四角标（同一 `Path` 几何旋转 90° 复用）+ 居中角色名。
   **严禁为立绘引入任何外部/云端图源**（离线红线），无图资产时保持工业占位形态。
7. 波形柱体美化 —— `RadiusX/Y="1.5"` 圆头 + `WaveformBarBrush` 线性渐变
   （顶部 `#38BDF8` → 底部 `#1E40AF`，竖直方向），替换纯色柱状图。

### 设计稿对齐精修（✅ 2026-09-16 第二轮，含主壳）

**新纪律（违反即回退设计稿质感，测试已固化）**

- **严禁 Unicode 伪字形当图标**：`⏮ ▶ ⏭ 🔄` 这类字符在字体回退后会渲染成大小形状不一的
  方框与裸三角。一律用 `ui:SymbolIcon`（`Previous24` / `Play24` / `Next24` / `ArrowSync24` …）。
- **徽章只能用低饱和暗钢蓝微阶**：`SpeakerPalette` 必须落在 #2A3C50 家族
  （单片色相跨度 ≤0x30、单分量 ≤0x80），配冷白文字 `#EEF8FF`。高饱和高光色块已被否决。
- **Inspector 恒为两张平级卡片**：`InspectorContextCard` / `InspectorGlossaryCard`，
  圆角 12，卡间透出底板；卡 1 内立绘与波形间必须有 1px `#252E42` 分隔线。

**StudioView.xaml**

- Inspector 双卡拆分；折叠改用**无实框裸箭头**（`Appearance=Transparent` + `Padding=4`）；
- 波形改为**中轴对称细波形**：`Width=2` / `Margin=2,0` / `VerticalAlignment=Center`，
  外层 `ItemsControl Height=56` 配 1px 中轴基线；柱高由 VM 的 `Math.Sin` 包络**自两端衰减**；
- 时长/补偿改**圆角深灰胶囊**；播放键统一 `42×32` `Appearance=Secondary` + `SymbolIcon` 内容；
- 表头文案补全 `Active Inline Editing` / `Quick Actions`；Re-try 换 `ArrowSync24`；
- Glossary 卡标题补 `ChevronUp24`，chip 去边框改 `#22262E` 圆角底。

**MainWindow.xaml / 外壳**

- 标题统一为 `Visual Novel Localization Studio`（`FluentWindow.Title` 与 `ui:TitleBar` 两处）；
- 导航槽 **4 项 + Footer**：`assets / studio / lqa / media / settings`，图标
  `Folder24 / Edit24 / CommentEdit24 / Filmstrip24 / Settings24`，标签改纯英文；
- 每项内容为两行：主标签 + `ACTIVE` 副标签（`Visibility` 由
  `IsActive` 经 `RelativeSource AncestorType=NavigationViewItem` 驱动，**仅激活项显形**）；
  项高 `MinHeight=46` / `Padding=12,8` —— 逐项属性设置而非 Style，
  规避「隐式样式覆盖 WPF-UI 主题样式导致丢失模板」的陷阱；
- 新增 `Pages/MediaPage.xaml(.cs)` 占位页并接入 `NavigateTo("media")`；
- 顶栏移除「打开」按钮，`Batch Translate` 升级 `ui:SplitButton`（`Appearance=Primary`），
  `Flyout` 内承载「打开剧本…/导出并编译…」（Popup 在独立可视树，
  故 `StackPanel` 用 `DataContext={Binding DataContext, ElementName=StudioRoot}` 显式锚回 VM）；
- 底栏重构：移除中段页面名；文件位置改**独立 Chip**（`Document24` + `StudioFileLabel`）；
  右侧补 `Path` 斜纹 **Resize Grip**；左侧文案 `Sidecar Engine: Ready`（含 Connecting/Fault 两态）；
- **新增内容区说明条**（设计稿有、原实现完全缺失）：左
  `Virtualized - DataGrid : compact ergonomics, macro protection` /
  右 `Clean vertical, collapsible Inspector` + `ChevronDown24`。

### 真实脚本加载与导出闭环（✅ 2026-09-16 第三轮，核心业务阶段一）

**RPC 面新增与方法表（`core/server/rpc.py`）**

- 注册方法扩为 7 个：`ping` / `fetch_models` / `detect_format` / `extract_to_ir` /
  `translate_batch` / `ir_to_asset` / **`run_lqa`** / `detect_engine` / `init_workspace` /
- **通用中继适配器（core/adapters/relay/）**：本地化工作站的引擎接入策略是
  「社区 CLI dump → 中间文本 → 通用适配器 → Gal-IR → 外部 CLI 回封」，**严禁再手写
  任何私有引擎解析器**。三适配器（Tabular/Marked/Json）实现 BaseEngineAdapter 并注册于
  `_ADAPTER_FACTORIES`（relay.tsv/relay.csv/relay.marked/relay.json），对 RPC/壳层零特例。
  格式与能力正交：引擎语义（dump/回编译工具、补丁形态）由 `recipes.py` 的 `RelayRecipe`
  声明（`supports_recompile=False` 时回封受控阻断 `RelayCapabilityError`）。保真纪律：
  pristine roundtrip 逐字节一致（newline='' + 行终止符探测 + BOM 对称保留 +
  JSON 行内手术替换**严禁 loads/dumps 全文往返**）+ 变异验证。
    `list_archives` / `unpack_archive` / **`convert_image`**。
- `_ADAPTER_FACTORIES`（模块级注册表常量）取代原先写死在 `build_default_dispatcher()`
  里的 `{"kag": KagAdapter()}` 字典；**新增引擎只改这张表**，值为构造器以保留
  「每次 dispatcher 独立实例」契约。键必须等于 `GalIRProject.engine_type` 字面量。
- `run_lqa(units) -> list[dict]`：**人工内联审校后的唯一复核入口**。复用
  `core.lqa.rules.run_static_rules`（零 LLM / 零 token / 零 I/O），刷新 `status`
  与 `metadata["lqa_issues"]`。三态语义：
  - 有译文 + 无 error → `LQA_PASSED`，并**清除**陈旧 `lqa_issues`（否则前台残留错误气泡）；
  - 有译文 + 有 error → `LQA_FAILED`；
  - **译文被清空 → 回归 `EXTRACTED`**（不得因标点规则整条跳过而误判为通过）。

**`_reinsert_tags` 锚点换算缺陷修复（`core/adapters/kag.py`，⚠️ 行为变更）**

- 根因：`tag.position` 相对 raw_text，而 **raw_text 本身包含这些宏**；`base` 是剥离宏后的
  正文。旧实现只减前缀长度，导致 multi-macro 行的锚点全部超出 `len(base)` 而被统一夹到
  行尾。实测 `tests/fixtures/sample_act1.ks` **未改动任何译文**即已破坏 2 行：
  - L15 `[font size=default]` 被后移 → 本应默认字号的整句留在 24 号字内；
  - L27 `[r]` 被后移 → 句中换行变成句末换行。
- 修复：逐个扣除「排在该宏之前的宏长度」，锚点才落在 base 坐标系上。
- **连带影响**：`tests/test_e2e_pipeline.py` 的 `lines[3]` 期望值随之更新为
  `【アリス】别哭呀[r][ruby text="はやく"]快点说`（`[r]` 与 `[ruby]` 原文相邻且零间隔，
  复写后必须仍相邻）。若看到这条断言变动，是修复而非回归。

**C# 宿主接通（`src/GalPipeline.UI.Wpf/`）**

- **彻底废除 `LoadDemoUnits()`**（12 行硬编码演示数据，其宏用的是设计稿风格的
  `<wait:300ms>`，与真实引擎语法不是一套）。首屏改由 `StudioView.Loaded` →
  `StudioViewModel.InitializeAsync()` → `LoadFileAsync(LocateSampleScript())` 走**真实
  RPC 链路**加载 `tests/fixtures/sample_act1.ks`。样本缺失时降级空态 + 提示，不崩启动。
- **失焦链路必须两步齐全**（`StudioView.xaml.cs::OnTranslationLostFocus`）：
  `item.CommitEdits()` → `await _vm.RecheckUnitAsync(item)`。只回写不复检，人工改坏的
  译文永远不会被拦下（`run_static_rules` 原本只在 `translate_batch` 内跑过一次，
  胶囊会永久停留在抽取期快照）。`RecheckUnitAsync` **刻意不走 `RunBusyAsync`**：
  审校时连续切行，全局 `IsBusy` 会让后续复检被 `if (IsBusy) return` 静默丢弃。
- **载入后状态复位**：`SearchText = ""` + `FilterMode = "All"`。否则上一个文件残留的
  筛选会把新数据整片滤空，表现为「加载失败」。胶囊由 Command 驱动、无 `IsChecked`
  双向绑定，故 VM 通过 `FiltersReset` 事件通知视图把 `FilterAll.IsChecked` 勾回。
- **导出闭环**：`ExportDirectory` 由 `ResolveExportDirectory()` 恒返回绝对路径
  （源文件同级 `localized/`，退化到仓库根）；成功后逐行 `MarkExported()` 推进
  `EXPORTED` 终态（契约里定义已久但此前**全仓库零写入**），并把产物路径经
  `LastExportPath` 回显到底栏 `Export: 目录/文件名`。
- **`EXPORTED` 的统计语义**：`IsPassed` 含 `EXPORTED`（计入已通过，否则进度条反向
  回退）；`IsPending` 排除 `EXPORTED`（否则导出行被算作待翻译）。
- **状态反馈面**：`StatusMessage` 此前是**零绑定的死属性**，载入失败/导出结果完全
  不可见。现经 `MetricsSink`（签名扩为 `Action<string,string,string>`）汇入
  `ShellViewModel.StudioStatusLabel`，绑定到内容区说明条；空闲时回落为设计稿原文
  `ShellViewModel.IdleHint`，保证静置时版面仍与设计稿一致。

### 批量翻译调度与进度联动闭环（✅ 2026-09-16 第四轮，核心业务阶段二）

**共享统计层（`src/GalPipeline.Core/Translation/TranslationProgress.cs`）**

公式必须落在这个**无 WPF 依赖的共享库**里，理由有二：

1. 壳层 `StudioViewModel` 属 `net10.0-windows`，测试工程是 `net10.0`（无 UseWPF）**无法引用**，
   公式留在 VM 里就只能靠 GUI 目测；
2. 「按钮角标计数」「批量选取目标」「进度条分子」是三处独立消费点，各写一份判定迟早漂移。

- `NeedsTranslation(status, text)`：**只认 `EXTRACTED` 且译文空白**。刻意不认 `LQA_FAILED` ——
  它携带的是「已翻译但质检不合格」这个**有效诊断**，重翻该行属单行 Re-try 职责；
  若计入待译，批量按钮会静默覆盖用户已看到的失败原因。空白以 `IsNullOrWhiteSpace` 判定。
- `Summarize(rows)`：单次遍历产出 `Total / Translated / Pending / Passed / Failed`。
- `TranslationProgressSnapshot.Ratio`：`Translated / Total`，**总数 0 时收敛为 0**（否则空表首屏 NaN）。
- `Text`：手工取整的 `"{N}% Translated"`。**不要改成 `ToString("P0")`** —— 它在 fr-FR 等
  区域性下会在数字与 % 之间插空格，令文案与断言都不可复现（已有 `[Theory]` 跨区域性锁定）。

**壳层联动（`StudioViewModel` / `StudioUnitItemViewModel`）**

- 行 VM 的 `NeedsTranslation` **委托**共享库实现；父 VM 订阅每行 `PropertyChanged`
  （只听 `TranslatedText` 与 `Status`）驱动整表重算，故角标与进度条随载入、回填、
  人工微调**实时**响应。
- <c>_suspendStats</c> 抑制闸：回填 N 行只末尾聚合一次，避免退化成 O(n²)
  （3500 行剧本下这一项就足以卡住 UI 线程）。
- **批量流水线**：`CommitEdits` 全量提交 → 按 `NeedsTranslation` 取目标 →
  `IsTranslating` 防重入 → **切片**（`TranslateChunkSize`，默认 8）循环
  `translate_batch` → `BackfillUnits` 按 id 原子回填 → 对**已获译文**的行
  `run_lqa` 复检 → 每片推进进度条 → 耗时与完成数落状态栏。
- **切片不是为绕过协议限制**，而是让进度条分档推进：整批一次发出，用户在整个往返期间
  只能看到进度条静止。切片时同步把 `BatchSize` 设为该片长度，避免与 Python 侧
  内部分批叠加成二次分批。
- **复检为何跳过无译文的行**：这类行由 `translate_batch` 判为 `LQA_FAILED`
 （`translator_protocol`，批次响应缺编号）；若一并送 `run_lqa` 会被规整成 `EXTRACTED`，
  洗掉这个有效信号。有测试 `test_run_lqa_downgrades_protocol_failure_to_extracted` 钉住该差异。
- 按钮可用性走 `CanTranslateBatch`（无在途 + 无全局忙 + 待译数 > 0），**不用** `IsIdle` ——
  后者无法反映批量在途，防重入会失效。

**⚠️ 连带修复：`translator._apply` 陈旧 LQA 留痕未清除（`core/pipeline/translator.py`）**

`_apply` 原先只在「本轮有违例」时写 `metadata["lqa_issues"]`，本轮无违例时**既不写也不清**。
后果：上一轮失败留下的 error 条目长期挂在**绿色的已通过胶囊**上 —— `IssueTooltip`
会据此把红色错误气泡画在绿底之上（单行 Re-try 修复后即可复现）。
修复：补 `else: unit.metadata.pop("lqa_issues", None)`。
已用**变异验证**确认断言非空转：删掉该分支后
`test_repair_then_retranslate_clears_stale_issue_metadata` 立即失败并打印出残留的违例内容。

### Inspector 行级动态数据联动（✅ 2026-09-16 第五轮，核心业务阶段三）

**架构决策：Inspector 的派生逻辑全部下沉到 `src/GalPipeline.Core/Inspector/`**

理由与上一轮的统计层同源：壳层 VM 属 `net10.0-windows`，`dotnet test` 工程
（`net10.0`，无 UseWPF）**引用不了它** —— 「切换 SelectedUnit 时 Inspector 状态是否正确」
若留在 VM 里，在无 GUI 纪律下就完全无法验证。落库后成为可直接断言的纯函数。

- `AudioCue.cs`：解析 `metadata["audio"]`（回退键 `voice`），接受两种形态 ——
  字符串简写 `"audio": "vo_00012.ogg"` 与对象 `{"file", "duration_ms", "comp_ms"}`；
  对象内同时容忍 snake_case / camelCase 与「数字写成字符串」。
  **判定口径**：取到任一已识别字段即视为有配音（只报时长、没报资源名也算），
  全无字段才判静音。
- `WaveformSampler.cs`：确定性包络。`Silence()` 返回 `BarCount=32` 根 `SilenceHeight=2px`
  的平直基线（读作中轴线上一条细线）；`ForVoice(seed)` 用 `sin` 包络令首尾收细，
  叠加以 seed 为源的固定伪随机扰动。
  **`StableSeed` 必须手写折叠，不能用 `string.GetHashCode()`** —— 后者在 .NET Core
  起每进程随机化，会导致「重启一次波形换一个样」。
- `InspectorState.cs`：整份快照（标题 / 角色卡 / 音频卡 / 波形 / 术语 Chip），
  不可变 record，`For(unit, glossary)` 一次求值。含：
  - `TitleFor` → `Inspector #00142`（与行号标签 `LineNumberTag` 同源，避免两处各写一份）；
  - `IsNarrationSpeaker`：`narrator` / `narration` / `旁白` / `ナレーション`（大小写不敏感）
    → 占位态，角色卡标注由「差分卡 · STANDING」切为「旁白 · NO SPRITE」；
  - 时长/补偿格式化用 `CultureInfo.InvariantCulture` 手写定型（`ToString("P0")` 与
    `#,0` 会随区域性漂移，上一轮已踩过）。

**壳层（`StudioUnitItemViewModel` / `StudioView.xaml`）**

- 行 VM 暴露 `Inspector` 快照 + 一组扁平转发属性（`SpeakerName` / `CharacterCardCaption` /
  `IsSpeakerPlaceholder` / `HasVoice` / `IsPlaybackEnabled` / `DurationLabel` / `CompLabel` /
  `Waveform` / `GlossaryMatches` / `GlossaryEmptyVisibility`）。
  **扁平而非 `SelectedUnit.Inspector.X` 嵌套**：绑定契约测试按「根属性必须在 VM 上」
  逐段解析，三层路径需要额外的作用域下沉支持；扁平形态零解析器改动且 XAML 更易读。
- `Speaker` / `SpeakerBadge` / `SpeakerBadgeBrush` / `LineNumberTag` **已改为委托 Inspector** ——
  故失效通知清单必须把它们一并列入（漏掉会出现「卡片变了、徽章没变」）。
- `InvalidateInspector()` 只在 **Model 整体替换**（`Update`）与**术语到位**（`ApplyGlossary`）时调用：
  `CommitEdits`（只改译文）与 `MarkExported`（只改状态）都不影响 Inspector 读到的字段，
  为它们付 17 次通知是纯浪费。
- 父 VM **不再持有** `WaveformBars` / `GlossaryItems` / `DurationLabel` / `CompLabel`，
  `RebuildInspector` 已删除 —— 选中行只是一次引用切换，Inspector 经 `SelectedUnit.X`
  直接读该行快照，不存在「父副本 vs 行数据」两处不一致。

**术语表（`core/tm/glossary.py` + `match_glossary` RPC）**

- 静态术语表 + **最长匹配**：同起点取更具体的那条（`指切りげんまん` 胜 `指切り`），
  被更长匹配覆盖的短术语直接丢弃（不重叠），结果按首现偏移升序。
  用 `str.find` 游标推进而非正则 —— 术语字面量可能含正则元字符，转义遗漏会静默漏配。
- 匹配依据是 `extracted_text`（原文），**抽取后不再变化** → 载入时整表一次 RPC、
  逐行分发缓存，切行时零往返。将来接 SQLite WAL 记忆库只需换 `core/tm` 实现，
  RPC 面与壳层消费方式不变。

**行内语音元数据（`KagAdapter` 的 `[voice ...]` 识别）**

- 识别形如 `[voice file="vo_00004.ogg" duration_ms=1850 comp_ms=200]` 的行内宏
  （`storage`/`src` 为 file 的别名），写入 `metadata["audio"]`。
- **只认整词 `voice`**：`se` / `bgm` / `play` 一律不算 —— 它们描述音效与背景乐，
  不构成「本行有人声」的证据，算进来会让音频卡对绝大多数行误报可播放。
- 纯增量字段：**不触碰 raw_text / extracted_text / atomic_tags**，
  故对回写保真与 LQA 锚点守恒零影响（有测试断言回写产物与源文件内容字节一致）。
- **如实记录**：内置样本 `tests/fixtures/sample_act1.ks` 不含 voice 宏，故 8 行全部走
  静音分支 —— 这不是缺陷，而是「适配器没报音频信息」时应有的表现。
  要看有语音态：用 `tests/fixtures/` 里带 `[voice ...]` 的脚本（拖入窗口即可）。

### 测试资产清单（三层防线）

**① `tests/test_xaml_binding_contract.py`（23 条，静态契约层）**
静态解析 **StudioView.xaml + MainWindow.xaml** 的绑定路径并分别对照
`StudioViewModel` / `StudioUnitItemViewModel` / `ShellViewModel` 的公共成员
（`DataTemplate` 内按最近 `ItemsSource` 判定元素作用域，`[RelayCommand]` 生成属性按命名规则静态推定），
并校验触发器 `SourceName` / `TargetName` 均指向已声明的 `x:Name`。
固化断言：静默态底线永不显示、原文/译文 Top 对齐 + 6px 留白、徽章胶囊 + 低饱和调色板、
Inspector 双卡平级、导航四槽顺序、无 Unicode 伪字形、本地资产已 `<Resource>` 嵌入
（拦「编译通过、运行崩溃」的 pack URI 陷阱）。
**业务闭环纪律**（本轮新增）：不得回退 `LoadDemoUnits`、不得复用相对导出目录常量、
失焦链路必须 `CommitEdits` 先于 `RecheckUnitAsync`、`EXPORTED` 的统计语义、
状态反馈面必须可见。
无 GUI 纪律下，这是唯一能自动拦住「绑定路径写错」「设计纪律回退」「业务回路断线」的防线。

**② `tests/test_e2e_rpc_roundtrip.py`（21 条，真实 JSON-RPC 帧层）**
不经 Python 函数直调适配器，而是把请求序列化成真正的 JSON-RPC 行帧喂给
`RpcDispatcher.handle_request` —— 覆盖壳层实际走的那条路（帧解析 → 参数绑定 →
异常映射 → 响应序列化）。八个断言面：引擎嗅探 / 单元抽取 / **骨架逐字节无损**
（未改动译文时回写产物与源文件「内容字节」完全一致）/ 宏守恒 / BOM 与无双回车 /
**往返幂等**（对产物再抽取，id·说话人·宏登记·正文集合全部一致）/ 异常帧拦截
（-32000 / -32601 / -32602 / -32700）/ `run_lqa` 损坏检出与修复放行。

**③ `tests/GalPipeline.Core.Tests/StudioRoundTripTests.cs`（7 条，C# IPC 层）**
纯 IPC 层驱动真实样本 `tests/fixtures/sample_act1.ks`：detect + extract 结构断言、
**未改动译文的原样回写必须内容字节一致**、人工改译文后骨架逐行原样 + 宏守恒 +
再抽取还原人工译文、`run_lqa` 三类语义。
**刻意不引用 WPF 项目**：该测试工程是 `net10.0`（无 UseWPF），引用 `StudioViewModel`
会编译失败；壳层 VM 行为由 ① 静态兜底。

**④ `tests/GalPipeline.Core.Tests/TranslationProgressTests.cs`（32 例，进度公式层）**
进度统计的边界全集：待译判定（含空白译文、编辑缓冲非空、`LQA_FAILED` 两种形态）、
空表除零收敛、`LQA_FAILED`/`EXPORTED` 的计数归属、**衰减单调性**（每落一条译文
待译严格减一）、`Ratio ∈ [0,1]` 全域扫描、`2/3 → 67%` 的设计稿取整口径、
以及 `[Theory]` 跨 fr-FR/de-DE/zh-CN/en-US 的区域性文案锁定。

**⑤ `tests/test_e2e_batch_translation.py`（10 条，批量翻译闭环层）**
走真实 JSON-RPC 帧 + 127.0.0.1 环回 LLM 桩（解析批次提示词按编号应答，可 `omit`
指定编号模拟协议失败）。断言面：空译文前置态、8 条回填与 `7 LQA_PASSED + 1 LQA_FAILED`
的门禁结果、**已通过单元不得残留违例留痕**、**修复后重翻必须清除陈旧留痕**（回归）、
待译计数衰减至 0、**内联门禁 ⇄ 独立 `run_lqa` 逐条一致**（两个门禁不一致则必有其一失效）、
分片往返的 id 对齐与 HTTP 往返计数、协议失败不得填入伪译文、
`run_lqa` 会把协议失败降级为 `EXTRACTED`（VM 侧过滤规则的依据）。

**⑥ `tests/GalPipeline.Core.Tests/InspectorStateTests.cs`（30 例，Inspector 派生层）**
`AudioCue` 解析全形态（字符串简写 / 对象 / camelCase / 数字写成字符串 /
键回退 / 空值 / 无法识别的键）、波形（静音恒定、同 seed 确定性、不同 seed 相异、
每柱可见、**分段均值验证首尾衰减**、稳定种子不受哈希随机化影响）、
`InspectorState`（空态、标题格式化与异常 id 兜底、具名/旁白/空角色三态、
**三行切换的完整字段矩阵**、有语音与无语音的波形数据分流、
同语音挂不同行产生同一波形、术语 Chip 格式化与 note 兜底）。

**⑦ `tests/test_inspector_context.py`（23 条，Inspector 数据来源层）**
术语匹配（最长优先、不重叠、偏移可回取字面量、重复出现各自上报、
空输入收敛、确定性、术语表自身良构）+ `match_glossary` RPC（按 unit_id 索引、
空项省略、空列表、只读 extracted_text、真实样本 8 行全部命中）；
行内语音元数据（完整参数 / `storage` 别名 / **`[se]` 与 `[bgm]` 不算配音** /
无资源名的 `[voice]` 忽略 / 旁白行不凭空获得线索 / **宏登记与正文剥离不受影响** /
**回写产物与源文件内容字节一致** / 内置样本如实为 0 条语音）。



### 工具链设置联动（动态消费出口）

- 设置快照新增 `CustomToolPaths`（IReadOnlyDictionary<ToolType,string>，null=未指定）；
  `TranslationSettingsStore.SetCustomToolPath(type, path)` 原子合并（空白=清除该条目）、
  内容防抖后触发 `ToolchainChanged`；`GetCustomToolPath(type)` 供设置页回显。
- **`GalPipeline.Core/Toolchain/ToolchainRuntime.cs`**：进程级动态解析器出口。
  仓库变更 → 解析器整体重建 + `ResolverChanged` 失效通知。**消费方严禁自建
  `ExternalToolResolver`**（契约测试钉死）—— StudioViewModel 走
  `ToolchainRuntime.Current`，FFmpegAudioCache 未显式注入（测试注入）时同样走运行时，
  并订阅失效通知清空 `_resolvedFfmpegPath`。
- **SettingsViewModel.PushToStore 必须携带 `CustomToolPaths`**（快照整体替换语义，
  不带 = 静默清空用户工具路径）—— 契约测试 `test_toolchain_settings_are_wired_to_runtime` 钉死。
- Settings 页「外部工具链环境」卡片：状态徽章（Ready+实测版本+来源 Custom/Local/PATH /
  Missing / Corrupted）、浏览配置（OpenFileDialog 限 ExecutableName）、恢复默认、
  打开目录（explorer /select）、全量重探；`ToolchainToolViewModel` 行级命令。



### EngineToolchainBridge（core/engine/bridge.py）

- 双向中继调度器：`dump_to_intermediate(recipe_key, source, output_dir, tool_path)` /
  `compile_from_intermediate(recipe_key, intermediate, target, tool_path)`。
  命令模板登记在 `RelayRecipe.dump_command / recompile_command`（**草案**，
  占位符 `{tool}/{input}/{output_dir}/{output}`，**必须独占 token** —— argv
  列表承载空格路径），krkr_psb / cs2 已登记；bgi 模板未校准 → 受控拒绝。
- 进程治理：60s 默认超时 → `taskkill /F /T` 树杀（POSIX kill）；输出走
  **临时文件而非 PIPE**（树杀后管道读取线程竞态是 pytest 线程警告来源，
  文件承载零竞态且超时路径也能带诊断尾部）；非零退出截获 stderr 尾 400 字符；
  dump 产物识别 = 后缀匹配 + mtime ≥ 启动时刻（**同名覆盖的新产物必须胜出**，
  路径差分会误拒）；compile 产物必须存在且非 0 字节。
- 能力闸门：compile 前先 `assert_can_recompile()`——supports_recompile=False
  时子进程零拉起；模板未校准配方同样受控拒绝。
- 工具路径由 C# `ToolchainRuntime` 解析后显式注入（单一事实源，Python 侧不重复探测）。
- 顺手修复：`core/media/pipeline.py` 外部转换模板 `{tool}` 未加引号 ——
  仓库路径含空格（"Gal-Pipeline Studio"）时 shell 截断（--basetemp 挪进
  仓库根后暴露的潜在缺陷）；`.pytest_temp/` 已入 .gitignore。



### 打包分发引擎（core/packaging/，模块七）

- **Dirty-Only 纪律**：`collector.collect_dirty_report(raw, localized)` 两级判定 ——
  size+mtime 双一致走免哈希快捷通道；其余一律 SHA-256 内容比对（**mtime 变化
  但内容未变必须判干净**）。removed 仅入 manifest 披露（增量覆盖无法表达删除，
  不进补丁载荷）。AssetDiff 自带 source 字段（严禁模块级注册表传状态）。
  `collect_dirty_assets()` 保留为兼容入口（同样已剔净中间产物）。
- **逆向中间产物机拦（D2 收口，2026-09-17 E2E 发现）**：`INTERMEDIATE_PATTERNS`
  内置样式表（`*.psb.json` / `*.resx.json` / `*.hazuki.txt` / `*.dump` / `*.tmp` /
  `*.bak` / `*~` / 兜底 `*.json`）。**最小误伤判据**：只拦「原版树中不存在」的
  **新增**文件 —— 原版里本来就有的 `.json` 属引擎原生资产，改动它可能是合法汉化，
  绝不擅自剔除；确有原生新增 `.json` 时用 `keep_globs` 显式放行。命中即
  WARNING 日志 + 从载荷剔除 + manifest `excluded_intermediates` 留痕（**剔除可见，
  不静默**）。护栏测试：`tests/test_intermediate_guard.py`（17 条）。
- **策略路由**：`PackagingPipeline.build(recipe, raw_dir=…, localized_dir=…,
  dist_dir=…, game_id=…, tool_path=…)` 按 patch_strategy 分派 ——
  OverlayPatch：暂存根 `{artifact}.d` 按原相对路径收纳 + pack_command 打包容器
  （未装配工具 → 暂存目录 + 装配指引的受控降级）；LooseDirectory：解压即用覆盖目录；
  XDeltaDiff：xdelta3 生成差分包 + **当场还原验证**（-d 回放哈希必须与汉化封包
  一致，不一致拒绝交付）。
  **参数顺序是硬契约**：`-e -s <原版> <修改版> <补丁>` / `-d -s <原版> <补丁> <还原产物>`
  —— 写反会得到 `XD3_INVALID_INPUT`（2026-09-17 真机 E2E 抓出的真产品缺陷 D1：
  单测替身曾把错误顺序一起固化，形成静默全绿）。
- **公共交付面**：`dist/{game_id}_patch/` = 策略载荷 + manifest.json（清单/哈希/
  打包状态）+ INSTALL.txt；交付根幂等重建（上次残留绝不混入）。
- 进程治理复用 bridge（`run_tool` 公共口；`_execute` 消息改 label 参数）。
- **环境事故记档**：系统 Python 的 pydantic-core 被外部改动为 2.49.0
  （pydantic 2.13.5 钉死 2.46.5）→ 全仓 import 崩溃；以
  `pip install pydantic-core==2.46.5` 精确回钉修复。测试基建与依赖版本
  是共享环境，跑测前发现 import 级崩溃先查环境再查代码。



### 工具链审计与真实装配（2026-09-17 联网权限裁决后）

- **用户裁决修正**：AGENTS.md「离线红线」约束的是**资产数据处理不外发**与
  GPL 代码零链接，不是禁止联网获取开源工具（此前系执行侧误读，已纠正）。
- **`scripts/audit_toolchain_dependencies.py`**：静态聚合 Python 配方
  （dump/recompile_tool）∪ C# 注册表（ExecutableName/ExpectedVersionArgs），
  扫 tools/ 工具箱 + PATH，输出 Found（路径/大小/实测版本）与 Missing
  （来源配方 + 预期调用命令 + 官方直链）。审计即测试：8 CLI 清单，
  现状 3 就绪（PsbDecompile/PsBuild/xdelta3）/ 4 缺失
  （ffmpeg/GARbro 因代理对大文件与 rar 不稳；cs2-tools/BGI-Tools 无官方稳定直链，
  按拒绝猜测原则不编 URL）。
  **2026-09-17 终态：8 就绪 / 0 缺失**（注册表 6 条目：FFmpeg / GARbro /
  FreeMote / XDelta / BGI / CatSystem2；配方侧追加 cs2_decompile + mc），
  详见 §2.5 的在线状态表。
- **`scripts/download_external_tools.py`**：GitHub Releases API 现查最新稳定资产
  （绝不硬编码版本），下载记 SHA256SUMS.txt，zip 完整性校验（EOCD，防截断包
  冒充成功），解压到 tools/{tool}/，console 工具启动探测（GUI 跳过）。
- **真机校准成果（实测推翻两处凭印象登记）**：FreeMote Toolkit v4.7.0 实际
  exe = PsbDecompile.exe / PsBuild.exe（**无** FreeMote.Psb.exe，PsbBuild.exe
  实为 PsBuild.exe）；两者均实测含 `-o|--output`（与已登记模板参数位吻合）；
  PsbDecompile 会同产 `*.resx.json` → bridge 产物识别补 stem 消歧规则。
  ToolchainRegistry.FreeMote 已修正（UlyssesWu 仓库 + 实测 SHA256）。
- **真机 E2E**：tests/test_krkr_psb_e2e.py::TestRealFreemoteToolchain（gated：
  tools/freemote/ 装配才跑）——真 PsbBuild→真 PsbDecompile→relay 译文→真
  PsBuild 回编译→真机复核译文落进二进制。ffmpeg/GARbro 网络通道对大文件
  不稳，重跑 download 脚本即可续拉。


### Milestone E2E 真实全链路穿透（✅ 2026-09-17，含 D1/D2 缺陷收口）

- **样本方案（诚实边界）**：仓内无商业资产 → **准真实样本**：真 `PsBuild` 编译真
  `.psb`（magic `PSB\0`）+ 真 PNG + 真 TLG5（仓库 tlg-rs 语义合成编码器，
  `core/media/tlg5.py` 闭环校验）。解包 / 回编译 / 差分**全部真机二进制执行，零替身**。
- **穿透链路**：`tests/e2e/test_real_pipeline_penetration.py`（16 用例 / 五阶段 + 整链路
  单跑，≈47 s）：真机 dump → Gal-IR(4 条) → LQA 门禁 → 外科回写 → 真机回编译 → 真机
  回读复核（译文在位 / 非文本节点守恒 / 宏恰好一次）→ TLG5 内置通道落 PNG → 图片资产
  入树 → Dirty-Only（3 项，未改动 TLG5 正确排除）→ OverlayPatch 暂存 + manifest 哈希
  逐项自洽 → XDeltaDiff（补丁 20 014 B / 封包 28 634 B，**独立回放哈希一致**）→
  免转区启动器（`-run` / `-runas`）+ 直启降级诊断。
- **阶段回显报告**：`.pytest_temp/e2e_penetration_report.json`（实测耗时 + 全量哈希），
  归档于 `docs/reports/e2e_penetration_run_2026-09-17.json`；验收报告
  `docs/reports/MILESTONE_E2E_PENETRATION_2026-09-17.md`。
- **D1（真产品缺陷，已修）**：`PackagingPipeline._build_xdelta` 还原验证参数位反向
  （`-d -s <补丁> <原版>`），真机报 `XD3_INVALID_INPUT`；单测替身把同一错误顺序一起
  固化 → 静默全绿。修复产品代码 + 替身语义对齐真实契约；真机 E2E 作为防漂移基线。
- **D2（资产边界漏洞，已机拦收口）**：中间产物（`scene.json` / `scene.psb.json`）
  混进资产树会被判 added 进补丁（Dirty 3→5 项）。收口：工作区纪律（中间件落 `work/`）
  + collector 机拦（见 §打包分发引擎）+ 交付面留痕 + 17 条护栏测试。
- **D3（认知修正）**：relay.json 管线**不登记宏** → 携带宏是 advisory(warning)；
  "全携带通过"属 KAG 管线语义。relay 的守恒保证来自真机回读「宏恰好一次」。
- **新模块 `core/launch/`**：LEProc 契约取自其二进制内嵌 Usage 实证
  （`-run path [args]` / `-runas guid path [args]` / `-manage` / `-global`，profile
  三级回退）。LE 属部署机外部工具，不入补丁包；缺失自动降级直启并如实诊断。

### 双进程验证命令（含环境前置条件）

- 前置 A：`dotnet build --no-incremental`（根目录，自动识别 `GalPipeline.slnx`）。
- 前置 B：**`python` 必须解析到装有 `pydantic` / `Pillow` 的解释器**（本机为 `C:\Python314`）。
  若 PATH 里优先级更高的解释器缺 `pydantic`，Sidecar 会在 import 阶段直接退出，
  `dotnet test` 的 IPC 用例会以 `-32603` / 「Sidecar 进程已退出」全红——
  **这是环境问题，不是代码缺陷**，先修 PATH 再怀疑代码。
- 前置 C：跑构建前**必须先关掉桌面端实例**，否则 `bin/` 被锁报 `MSB3027`（同样不是代码缺陷）。
- `dotnet test --no-build`（C# 用例 **139 条**，含 Toolchain 注册表元数据契约）
- `python -m pytest tests/ --basetemp=.pytest_temp`（Python 全量回归 **453 条**，
  含真机 FreeMote E2E、16 条真实全链路穿透、17 条中间产物机拦护栏）

### 后续路线（沿用既定规划）

- Settings 页：模型下拉接入 `fetch_models` 真实数据 + `reasoning_effort` 分段控件持久化；
- LQA Lab：汇总 LQA_FAILED 单元的复检工作区；
- Asset Hub：封包/图片/音频归档与预览；
- 主外壳：Token 用量真实计量（当前为占位）；
- Studio：把 `SelectedReasoningEffort` 从 Settings 透传进 `TranslationConfigDto.ReasoningEffort`
  （字段已在契约里，当前批量翻译未注入）；
- 适配器：PSB / HG3 引擎实现（注册表已就绪，加 `_ADAPTER_FACTORIES` 一项即可）；
- 术语表 → **翻译 Prompt 注入**：`core/tm` 目前已能产出术语约束，但只用于 Inspector 展示，
  尚未进译者 prompt（这是术语表真正的价值出口，建议下一轮做）。
- Inspector 波形：接真实音频解码，替换 `WaveformSampler.ForVoice`（调用方签名不变）；
- Inspector 播放三键：当前只有启用态，**无播放实现** —— 需要音频解码与播放设备接入；
- 适配器音频来源：`[voice ...]` 行内宏只是缺省兜底，更可靠的是封包伴生语音索引
  （`@voice` 命令行 / 语音清单），由 reverse-engine 侧评估。
