# Gal-Pipeline Studio 智能体系统架构与总则

## 1. 架构定位
本项目旨在构建工业级 Galgame 本地化流水线，采用 WinUI 3 (C#/.NET 8) 桌面客户端作为壳层，通过无缓冲 StdIO JSON-RPC 2.0 调度 Python 核心翻译与质量引擎。所有业务逻辑遵循严密的契约先行、数据流向隔离与分支发布机制。

## 2. 角色花名册与管辖边界
* **Agent 0: 系统架构协调官（主 Agent）**
  - 模型：glm-5.3 | 思考强度：high | 权限：完全权限
  - 核心职责：统揽全局架构，唯一有权定义与修改系统核心契约 (`core/models/ir.py`, `core/adapters/base.py`, `docs/ipc_protocol.md`)。
* **Agent 1: reverse-engine（引擎逆向与资产适配专家）**
  - 模型：glm-5.3-flash | 管辖：`core/adapters/`, `tools/`
  - 职责：封包解包、字节码读写、BMP 内存压榨、HG3/PSB 伴生坐标防呆。
* **Agent 2: nlp-pipeline（语言学与模型调度专家）**
  - 模型：glm-5.3-flash | 管辖：`core/pipeline/`
  - 职责：形态素分词、母子串拓扑树、本地 SQLite WAL 记忆库、Prompt 组装。
* **Agent 3: lqa-watchdog（静态 LQA 守门狗工程师）**
  - 模型：glm-5.3-flash | 管辖：`core/lqa/`
  - 职责：100% 确定性纯算法校验、控制符守恒、标点符号规范化配平、编码跨界阻断。
* **Agent 4: winui-developer（WinUI 3 原生客户端工程师）**
  - 模型：glm-5.3-flash | 管辖：`src/`
  - 职责：WinUI 3 Unpackaged 桌面客户端、Mica Alt 材质、Sidecar 生命周期托管。
* **Agent 5: git-releaser（版本发布与推送守门人）**
  - 模型：glm-5.3-flash | 权限：只读与终端执行（无写权限）
  - 职责：自动化测试前置拦截、语义化版本打 Tag、GitHub 远端同步唯一出口。

## 3. 技术红线与一票否决项
1. **核心契约不可变**：除 Agent 0 外，任何子 Agent 严禁修改 `core/models/ir.py` 与 `core/adapters/base.py`。
2. **离线安全红线**：多媒体资产处理 100% 本地离线，严禁引入任何向云端发送图像数据的代码。
3. **GPL 物理隔离**：外部依赖（FreeMote、FFmpeg 等）必须作为独立进程通过 CLI 跨进程调用，严禁源码引用或静态链接。
4. **测试与推送解耦**：日常开发智能体（Agent 1~4）仅负责编写与本地自测，严禁直接执行 `git push`；代码必须经由 Agent 5 验收全绿后统一推送到 GitHub。

## 4. 标准作业流（本地构建与版本流闭环）
1. **契约定义**：Agent 0 敲定数据模型与接口抽象；
2. **模块研发**：专业子 Agent 在其独立上下文中编写代码并本地调试；
3. **本地自测**：执行 `pytest tests/` 或 `dotnet build` 保证 100% 通过；
4. **验收发布**：由 `git-releaser` 执行测试验收、暂存提交、打上 SemVer Tag 并推送至 GitHub 远端仓库。
## 当前交接状态索引
- 新接手本项目的 AI 请先完整阅读 [docs/AI_HANDOFF.md](docs/AI_HANDOFF.md)，严格遵守无 GUI 测试纪律，按文档指引继续推进。
