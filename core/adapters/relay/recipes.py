"""中继配方体系 —— 「引擎 × 社区工具」能力边界的声明式登记处。

架构定位（Agent 0 技术路线裁决）::

    本地化工作站 ≠ 底层逆向工程库。私有引擎的 dump/编译一律交给
    社区成熟 CLI（GARbro / FreeMote / cs2-tools / BGI-Tools ...），
    本仓只实现**通用中继适配器**（Tabular / Marked / Json → Gal-IR）。
    每个可支持的引擎 = 一条 ``RelayRecipe``：声明用哪个工具 dump、
    中间文本归哪类适配器、工具链是否具备回编译能力、交付补丁形态。

能力边界的诚实原则::

    ``supports_recompile=False`` 的配方（如纯 GARbro 只读解包）在回封
    阶段必须**受控阻断**（<see RelayCapabilityError>），绝不静默产出
    无法被引擎加载的半成品 —— 「没有编译器」是系统里的一等事实，
    由 UI/桥接层如实呈现，而不是藏在代码路径里。
"""

from dataclasses import dataclass

from ..base import EngineAdapterError

# ---------------------------------------------------------------------------
# 补丁交付策略（patch_strategy 的合法值域）
# ---------------------------------------------------------------------------

PATCH_OVERLAY = "OverlayPatch"
"""增量覆盖包：引擎原生补丁容器（如 KiriKiri patch2.xp3、CS2 update.int）。"""

PATCH_LOOSE = "LooseDirectory"
"""散文件直换：解包目录内原位替换（BGI SysScript.arc 重编译产物等）。"""

PATCH_XDELTA = "XDeltaDiff"
"""差分补丁：对原版资产做 VCDIFF 增量（交付链末端的打包通道）。"""


class RelayCapabilityError(EngineAdapterError):
    """配方不具备回封能力（supports_recompile=False）时的受控阻断。

    继承 EngineAdapterError：JSON-RPC 错误映射层统一识别为 -32000，
    携带的引导文案直达用户（去装配带编译器的社区工具或改用其它配方）。
    """


@dataclass(frozen=True)
class RelayRecipe:
    """一条「引擎 × 社区工具」中继配方：能力边界的唯一事实来源。

    适配器（relay.tsv / relay.marked / relay.json）只懂**中间文本格式**，
    与具体引擎无关；引擎语义（用什么工具 dump、能不能回编译、补丁长
    什么样）全部由本结构声明 —— 格式与能力正交，是这套架构的核心解耦。
    """

    key: str
    """配方主键（如 ``krkr_psb``）。"""

    engine_label: str
    """界面可读的引擎名（如 ``KiriKiri / PSB``）。"""

    adapter_key: str
    """绑定的中继适配器键（relay.tsv / relay.marked / relay.json）。
    纯只读兜底配方无中间文本形态，为空串。"""

    dump_tool: str
    """负责导出中间文本的社区 CLI（展示名；实际路径由 C# 工具链解析传入）。"""

    recompile_tool: str | None
    """负责回编译的社区 CLI；不支持回封时为 None。"""

    supports_recompile: bool
    """工具链是否具备回写/回编译能力（有编译器 True；纯 GARbro 只读 False）。"""

    patch_strategy: str
    """交付形式：PATCH_OVERLAY / PATCH_LOOSE / PATCH_XDELTA。"""

    patch_artifact: str | None
    """补丁产物名（patch2.xp3 / update.int / SysScript.arc ...）；散文件交付可为 None。"""

    notes: str = ""
    """补充说明（Known limitations / 装配指引）。"""

    dump_command: str | None = None
    """dump CLI 命令模板（**草案**，接入真实工具时按实测校准——同 GARbro
    版本参数先例）。占位符：``{tool}`` / ``{input}`` / ``{output_dir}``。
    规范约束：**占位符必须独占 token**（空格分隔），路径含空格才能被
    argv 列表正确承载。None = 模板未校准，调度器受控拒绝。"""

    recompile_command: str | None = None
    """回编译 CLI 命令模板。占位符：``{tool}`` / ``{input}``（中间文件）/
    ``{output}``（目标产物文件）。None = 模板未校准。"""

    pack_command: str | None = None
    """增量容器打包 CLI 模板（OverlayPatch 交付用）。占位符：``{tool}`` /
    ``{patch_dir}``（暂存根）/ ``{output}``（容器产物，如 patch2.xp3）。
    None = 未登记：打包管道降级为「暂存目录 + 装配指引」交付，不阻断。"""

    templates_calibrated: bool = False
    """命令模板是否已经**真实工具实测校准**（True = 按官方 CLI 规范登记；
    False = 草案，接入真实工具后必须实测校准并翻转本标记）。

    引号规范：bridge 以 argv 列表起子进程（非 shell 字符串），路径含
    空格由列表承载 —— 模板内**严禁**给占位符加引号（引号会成为参数
    字面量），这正是与 Windows 环境 100% 契合的形态。"""

    @property
    def intermediate_suffix(self) -> str | None:
        """该配方的中间文本产物后缀（由绑定适配器推导，供桥接器识别产物）。"""
        return {
            "relay.json": ".json",
            "relay.tsv": ".tsv",
            "relay.csv": ".csv",
            "relay.marked": ".txt",
        }.get(self.adapter_key)

    def assert_can_recompile(self) -> None:
        """回封前置闸门：不具备回编译能力时受控阻断。

        由 EngineToolchainBridge（后续切片）在调用编译 CLI 前执行；
        适配器回写中间文本不受此闸门约束（中间文件本身总是可产出的）。
        """
        if not self.supports_recompile:
            raise RelayCapabilityError(
                f"配方 {self.key}（{self.engine_label}）不具备回编译能力："
                f"社区工具链只有只读 dump（{self.dump_tool}），没有可用的回封通道。"
                f"中间文本与译文仍可正常产出（交付形式：{self.patch_strategy}），"
                "但请装配带编译器的社区工具后再执行回封。"
            )


RECIPES: dict[str, RelayRecipe] = {
    "krkr_psb": RelayRecipe(
        key="krkr_psb",
        engine_label="KiriKiri / PSB",
        adapter_key="relay.json",
        dump_tool="PsbDecompile.exe",
        recompile_tool="PsBuild.exe",
        supports_recompile=True,
        patch_strategy=PATCH_OVERLAY,
        patch_artifact="patch2.xp3",
        notes="FreeMote 的 PSB↔JSON 往返成熟可靠；回编译产物以增量补丁包 patch2.xp3 交付。",
        dump_command="{tool} -o {output_dir} {input}",
        recompile_command="{tool} {input} -o {output}",
        pack_command="{tool} {patch_dir} -o {output}",  # Xp3Pack 风格草案
        templates_calibrated=True,
    ),
    "cs2": RelayRecipe(
        key="cs2",
        engine_label="CatSystem2",
        adapter_key="relay.marked",
        dump_tool="cs2_decompile.exe",
        recompile_tool="mc.exe",
        supports_recompile=True,
        patch_strategy=PATCH_OVERLAY,
        patch_artifact="update.int",
        notes="实测校准（2026-09-17）：dump=cs2_decompile（真实开关 -i <inputs> "
              "-o <output_dir>，-u|--utf8 / -hr|--human / -md|--markdown）；"
              "recompile=mc.exe（官方 Message Compiler，编译方向待真实游戏样本 E2E 复核）。"
              "辅助 CLI（tools/cs2/）：exkifint_v3（int 封包解密，需配 game.exe 密钥）、"
              "hgx2bmp（HG 图像转 bmp）、exzt（zt 封包）；增量走引擎原生 update.int 覆盖包。",
        dump_command="{tool} -i {input} -o {output_dir} -md",
        recompile_command="{tool} {input}",  # 草案：mc 编译方向按官方用法登记，待 E2E 校准
    ),
    "bgi": RelayRecipe(
        key="bgi",
        engine_label="BGI / Ethornell",
        adapter_key="relay.tsv",
        dump_tool="BGI_Hazuki_TextTool.exe",
        recompile_tool="BGI_Hazuki_TextTool.exe",
        supports_recompile=True,
        patch_strategy=PATCH_LOOSE,
        patch_artifact="SysScript.arc",
        notes="实测校准（2026-09-17，二进制内嵌用法实证，零猜测）："
              "dump=extract <script> [output.hazuki.txt] [--decode-cp 932]；"
              "recompile=apply <script.hazuki.txt> [output_script] [--encode-cp 932]。"
              "文件型输出占位符接入 bridge 后再启用 dump_command/recompile_command。"
              "封包解包由 BGI_Unpacker（BURIKO ARC20，拖拽式交互）承担，暂不入无头链。"
              "TSV 双栏对照；回编译产物原位替换解包目录内 SysScript.arc（散文件交付）。",
    ),
    "generic_garbro": RelayRecipe(
        key="generic_garbro",
        engine_label="通用（GARbro 只读兜底）",
        adapter_key="",
        dump_tool="GARbro.Console.exe",
        recompile_tool=None,
        supports_recompile=False,
        patch_strategy=PATCH_LOOSE,
        patch_artifact=None,
        notes="实测校准（2026-09-17）：GARbro.Console 真实开关 -l（列格式）/ "
              "-x <archive>（全量解包，输出至 CWD，无 -o 选项）/ -c FORMAT（图像转换）。"
              "GameData/Formats.dat 须与 exe 同级。绝大多数格式只读：可解包取证，"
              "但回封被受控阻断（RelayCapabilityError）。CWD 语义接入 bridge 后启用 dump_command。",
    ),
}


def get_recipe(key: str) -> RelayRecipe:
    """按主键取配方；未知键受控报错（派生自 EngineAdapterError）。"""
    recipe = RECIPES.get(key)
    if recipe is None:
        raise RelayCapabilityError(
            f"未知中继配方：{key}（已登记：{', '.join(sorted(RECIPES))}）"
        )
    return recipe
