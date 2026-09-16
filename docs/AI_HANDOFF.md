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
  支持 `reasoning_effort` 推理力度）→ LQA 即时门禁（引号配平/破折号成双/锚点守恒）→ `ir_to_asset`
  原位回写（骨架行逐字保真 + 宏逐字节复写 + UTF-8 BOM）。

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
    `ping` / `fetch_models` / `detect_format` / `extract_to_ir` / `translate_batch` / `ir_to_asset`。
  - 适配器层 `core/adapters/`：`KagAdapter`（KAG .ks 剧本）+ `image.py`（RGBA PNG-32 离线归一化）。
  - 翻译调度 `core/pipeline/translator.py`：OpenAI 兼容批次协议（编号行 `N. 【角色】正文`）、
    `reasoning_effort` 注入、清洗纪律（**严禁触碰「」引号**——那是 LQA 的断言靶子）。
  - LQA 门禁 `core/lqa/rules.py`：引号栈扫描配平 / 破折号成双游程 / 宏锚点守恒，
    error 级阻断、warning 级放行留痕（`metadata["lqa_issues"]`）。

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
- **WPF-UI 4.3 专属坑**：
  - `SelectionChanged` / `ItemInvoked` 事件链在本宿主形态下不触发——导航统一走
    `OnPaneMouseUp` + `VisualTreeHelper.HitTest` 命中测试（`MainWindow.xaml.cs`）；
  - 包内不存在 WinUI 生态的 `SettingsCard`，用 `CardControl` / `CardExpander`；
  - `StackPanel` 无 `Spacing`；数据模板内严禁 `x:Bind`（WPF 侧用经典 `{Binding}` + INPC）。

## 4. 当前基线与待办任务

- **分支**：`feature/fluent-wpfui-architecture`（main 已同步至 `v0.7.0` 里程碑）。
- **构建状态**：`dotnet build --no-incremental` 0 警告 0 错误；测试全绿（C# 8/8，Python 159）。
- **双进程验证命令**：
  - `python -m pytest tests/`（Python 全量回归）
  - `dotnet test`（C# IPC 进程通信）
  - `dotnet run --project src/GalPipeline.UI.Wpf`（拉起桌面端 + 自动拉起 Sidecar）
- **本地离线演练资产**：`tests/fixtures/sample_act1.ks`（真实 KAG 样本）+
  `tests/fixtures/mock_llm_server.py`（127.0.0.1:18080 环回 Mock LLM，零额度）。

### StudioView 待办视觉微调

1. Col 3 译文输入框：优化聚焦效果，获得焦点时展示 Fluent 2 亮蓝底部指示线；
2. Col 4 状态胶囊：为 ✓ Passed / ⊗ Failed / Draft 增加 1px 半透明 Keyline 细描边；
3. Col 1 角色徽章：格式化为 `[Speaker]` 补齐方括号；
4. 右侧 Inspector：标题整合为单行 `Inspector #00142`，移除冗余行号。

### 后续路线（沿用既定规划）

- Settings 页：模型下拉接入 `fetch_models` 真实数据 + `reasoning_effort` 分段控件持久化；
- LQA Lab：汇总 LQA_FAILED 单元的复检工作区；
- Asset Hub：封包/图片/音频归档与预览；
- 主外壳：Token 用量真实计量（当前为占位）。
