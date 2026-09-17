# Milestone E2E 真实全链路穿透 · 验收报告

- **日期**：2026-09-17
- **范围**：解包 → 剧本提取 → 图像流转 → 外科回写 → 增量/差分打包 → 免转区启动引导
- **执行方式**：纯命令行无头自动化（无 GUI、无人工干预），16 条 E2E 用例
- **证据归档**：`docs/reports/e2e_penetration_run_2026-09-17.json`（本次运行实测耗时与哈希）

---

## 一、样本方案（诚实边界）

仓库内**没有也不应有**商业游戏资产（版权 + 不可复现）。因此本里程碑采用
**引擎原生格式的准真实样本**：剧本容器由**真实 FreeMote `PsBuild.exe`** 从语义
JSON 编译为真 `.psb`（magic `PSB\0`，720 B），图像包含真实 PNG 与真实 TLG5
（仓库内 tlg-rs 语义合成编码器，由 `core/media/tlg5.py` 解码器闭环校验）。

**所有解包 / 回编译 / 差分动作全部由真实二进制完成，零替身**：

| 工具 | 版本/形态 | 用途 |
|---|---|---|
| `tools/freemote/PsbDecompile.exe` | FreeMote Toolkit v4.7.0 | 真机 dump（PSB → JSON） |
| `tools/freemote/PsBuild.exe` | FreeMote Toolkit v4.7.0 | 真机回编译（JSON → PSB） |
| `tools/xdelta/xdelta3.exe` | Xdelta 3.2.0 | 真机差分 encode / decode |
| `tools/locale_emulator/LEProc.exe` | Locale Emulator 2.5.0.1 | 免转区启动契约（`-run` / `-runas`） |

> CatSystem2 侧未纳入本轮主线：仓内无真实 `.int/.cst` 商业样本可验，
> 其命令行契约已由 `tests/test_engine_bridge.py` 的模板测试覆盖；真实样本
> 到位后可用同一套 E2E 骨架接入（`cs2_decompile` / `mc`）。

---

## 二、阶段报告摘要（实测耗时 + 哈希校验）

单次完整穿透 **≈46.7 s**（含 17 次阶段回显；下表为各阶段首次达成的累计耗时）。

| 阶段 | 累计耗时 | 流转结果 | 哈希/校验证据 |
|---|---|---|---|
| 0 样本装配 | 3.86 s | 真机 `PsBuild` → `scene.psb` 720 B | `psb_sha256=6d7d7609…42e6a5d` |
| 0 图像样本 | 5.49 s | 真 PNG + 真 TLG5（双格式） | `png=250e116e…ffcd7f0d` / `tlg=26381096…6c73624c`（`tlg_format=TLG5` 嗅探通过） |
| 1 资产提取 | 8.33 s | 真机 `PsbDecompile` → `scene.json` 550 B（`resx.json` 由 stem 消歧胜出） | `dumped_sha256=989199b1…ba544f41` |
| 2 Gal-IR 抽取 | 11.16 s | 4 条对白，角色归属 `[千代, 千代, null, 健一]` | `units=4` |
| 2 LQA 门禁 | 14.06 s | relay 管线携带宏 → 2 条 advisory（不阻断） | 见缺陷 D3 |
| 2 外科回写 + 真机回编译 | 19.35 s | 译文落进真二进制，原文消失，非文本节点守恒 | `source=6d7d7609…` → `patched=423154f3…508d7`（720 B）；`macro_occurrences=1`；`non_text_nodes_preserved=true` |
| 3 TLG5 → PNG | 20.96 s | 内置解码通道 64×32 → PNG 167 B | `tlg=26381096…` → `png=6e879372…b2276a0` |
| 3 图片资产入树 | 22.55 s | 1 张同名覆盖（改像素）+ 1 张新增 | `原=250e116e…` / `改=b135cabf…55f8ea` / `增=7f22c7d9…41f19a4` |
| 4 Dirty-Only 收集 | 26.67 s | **3 项**，未改动 TLG5 被正确排除 | `untouched_excluded=true`；三项 SHA-256 全量记录 |
| 4 暗礁探测 | 28.23 s | 误入资产树的中间产物被识别为 `added`（可拦截） | 见缺陷 D2 |
| 4 OverlayPatch 交付 | 32.71 s | 暂存 3 文件 + manifest 哈希逐项自洽 | `manifest_sha256_verified=3`，`manifest_dirty_count=3` |
| 4 XDeltaDiff 差分 | 36.99 s | 补丁 **20 014 B** / 汉化封包 **28 634 B**（69.9%）；**独立回放哈希一致** | `orig=e9517f02…` / `loc=291573c2…` / `patch=866ba12f…` / `replay=291573c2…` → `replay_hash_match=true` |
| 5 免转区契约 | 38.27 s | `-run <game>` 与 `-runas <guid> <game> [args]` 双形态、参数位序正确 | LEProc 二进制内嵌 Usage 实证 |
| 5 启动器落盘 | 39.94 s | `启动游戏.bat`（转区优先 + 直启回退）+ 诊断摘要 | `localized_launch_ready=true` |
| 5 降级路径 | 41.65 s | LEProc 缺失 → 直启 + 诊断如实披露（不静默失败） | — |
| 整链路 | 46.65 s | 全阶段单次运行穿透（无人工干预） | 736 B PSB 交付 |

---

## 三、缺陷与修复清单（本轮穿透挖掘）

### D1 · xdelta3 还原验证参数位反向（**真产品缺陷**）

- **现象**：`PackagingPipeline._build_xdelta` 的还原验证命令把差分包当成源文件：
  `xdelta3 -d -f -s <补丁> <原版> <还原产物>`。真机执行报
  `xdelta3: not a VCDIFF input: XD3_INVALID_INPUT`，**任何 XDeltaDiff 交付都会在真机上失败**。
- **为什么单测没发现**：`tests/test_packaging_pipeline.py` 使用行为替身 `_xdelta_shim`，
  而替身**把同一个错误参数序一起固化了** —— 形成"静默全绿"。
- **修复**：
  - `core/packaging/pipeline.py`：改为真实契约 `-d -f -s <原版完整封包> <补丁> <还原产物>`，
    并在注释中写明「参数顺序是硬契约，写反即 `XD3_INVALID_INPUT`」。
  - `tests/test_packaging_pipeline.py`：替身参数位置对齐真实 xdelta3 契约，docstring 标注
    `-e -s <原版> <修改版> <补丁>` / `-d -s <原版> <补丁> <还原产物>`。
- **防线**：本 E2E 用**真实 xdelta3 3.2.0** 执行生成 + 管道内还原验证 + **独立回放复核**
  （不复用管道内部验证，`replay_hash_match=true`），并与替身单测形成"真机 + 替身"双轨。

### D2 · 中间构建产物污染增量补丁的资产边界漏洞

- **现象**：dump 中间件 `scenario/scene.json` 与回写中间件 `scenario/scene.psb.json`
  一旦落进汉化资产树，**Dirty-Only 收集器会把它们判为 `added` 资产**（实测 Dirty 由 3 项
  膨胀到 5 项），随补丁交付给玩家 —— 补丁污染 + 体积浪费。
- **暴露路径**：本测试首次装配样本时 `copytree` 早于中间件清理，Dirty 集合立刻多出 2 项
  （`scenario/scene.json`、`scenario/scene.psb.json`），**说明该错配是真实流水线极易踩的雷**。
- **修复（纪律 + 守卫）**：
  - 固化**工作区纪律**：dump/回写中间件必须落 `work/`，只有最终资产进资产树
    （`PenetrationSample.work` + `_apply_translation_and_recompile` 统一收口）；
  - 新增**交付守卫断言**：OverlayPatch 暂存树中不得出现任何 `.json` 中间产物；
  - 新增**暗礁固化用例** `test_stray_intermediate_in_asset_tree_is_detectable`：
    证明该错配可被 Dirty-Only 探测（非静默污染）。
- **产品侧建议（待裁决，本轮未实施）**：`collector` 可对已知中间产物扩展名
  （`*.psb.json` / `*.resx.json` / `*.json`）发出 warning 级提示，把"人踩"提前到"机拦"。

### D3 · relay 管线控制符语义认知修正（测试假设错误，非产品缺陷）

- **现象**：E2E 初期断言「译文全携带 `[ruby]` 宏 → LQA 零违例」失败，实测返回 2 条 warning。
- **真相**：KAG 管线是「剥离式 + 宏登记表」（全携带 = 通过）；而 **relay.json 管线不登记宏**
  （文本整串透传，适配器不解释引擎语法），因此携带宏被如实判为 advisory，不升级为 error。
- **修复**：测试按真实语义重写（断言 advisory 且 severity 全为 warning），并明确
  **relay 管线真正的守恒保证来自"真机回读宏字面量恰好一次"**（`macro_occurrences=1` 已验证）。

### D4 · 替身—真机契约漂移（防线补强）

- **教训**：D1 的根因是替身模仿了"实现"而非"外部契约"。
- **补强**：新增真实工具 E2E（本文件）作为防漂移基线；此后任何 dump/编译/差分 CLI 契约变更，
  真机穿透会立刻红。

---

## 四、三线最终数据

| 验证线 | 命令 | 结果 |
|---|---|---|
| 构建 | `dotnet build --no-incremental` | **0 警告 / 0 错误** |
| C# 测试 | `dotnet test --no-build` | **139 / 139 通过** |
| Python 测试 | `pytest tests/ --basetemp=.pytest_temp` | **436 passed**（420 → 436，新增 16 条 E2E），**零警告** |

---

## 五、本轮新增/修改产物

| 文件 | 类型 | 说明 |
|---|---|---|
| `tests/e2e/test_real_pipeline_penetration.py` | 新增（673 行 / 16 用例） | 五阶段真实穿透 + 整链路单跑 |
| `core/launch/__init__.py`、`core/launch/launch_config.py` | 新增（209 行） | LaunchConfig：免转区命令构造 / 启动器渲染 / 降级诊断 |
| `core/packaging/pipeline.py` | **修复** | xdelta 还原验证参数序（D1） |
| `tests/test_packaging_pipeline.py` | 修复 | 替身语义对齐真实契约（D1/D4） |
| `docs/reports/e2e_penetration_run_2026-09-17.json` | 新增 | 本次运行证据（耗时 + 全量哈希） |

---

## 六、复现命令

```bash
# 单跑真实穿透（无 GUI；真实工具缺失时整文件跳过并说明原因）
python -m pytest tests/e2e/test_real_pipeline_penetration.py -q --basetemp=.pytest_temp
# 查看结构化阶段回显
python -m pytest tests/e2e/test_real_pipeline_penetration.py -s -q --basetemp=.pytest_temp
cat .pytest_temp/e2e_penetration_report.json
```

---

## 七、遗留事项

1. **CatSystem2 真实样本穿透**：需商业 `.int/.cst` 样本，骨架已就绪（`cs2_decompile` / `mc`）。
2. **collector 中间产物 warning**（D2 建议）：属产品行为变更，待用户裁决。
3. **GARbro.Console / BGI_Hazuki / xdelta 之外的 8/8 CLI** 中，GARbro 与 BGI 未纳入本轮主线
   （无对应真实封包样本；GARbro 只读解包链路已在 `generic_garbro` 配方中受控阻断）。
4. **LEProc 为部署机外部工具**：不随补丁分发包分发，启动器已内置"未装则直启"回退与诊断。
