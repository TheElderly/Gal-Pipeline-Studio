namespace GalPipeline.Core.Toolchain;

/// <summary>
/// 预设工具链清单 —— 系统认知的「外部工具链资产」唯一登记处。
///
/// 新增外部依赖（如 future 的 YuriSizuku / arc_unpacker）只改这里，
/// 探测器、一键下载器、Missing 引导全部自动跟随，**严禁**在业务代码里
/// 再手写任何一条「找 ffmpeg / 找 garbro」的特例逻辑。
///
/// 版本探测口径说明：FFmpeg 的 ``-version`` 与输出格式是稳定的公开契约；
/// GARbro / FreeMote / XDelta 的命令行版本参数与输出格式随发布版浮动，
/// 当前登记的是**草案**，接入真实发布包时按实测校准（元数据是纯数据，
/// 校准零代码改动）。
/// </summary>
public static class ToolchainRegistry
{
    public static IReadOnlyList<ExternalToolInfo> DefaultTools { get; } =
    [
        new ExternalToolInfo(
            Type: ToolType.FFmpeg,
            DisplayName: "FFmpeg",
            ExecutableName: "ffmpeg.exe",
            RelativeInstallDir: "tools/ffmpeg/",
            DownloadUrl: "https://github.com/BtbN/FFmpeg-Builds/releases",
            ExpectedVersionArgs: ["-version"],
            VersionPattern: @"ffmpeg\s+version\s+(\S+)",
            IsMandatory: true,
            Purpose: "多媒体唯一底层：语音转码（OGG→WAV）、OP/ED 提取与字幕压制均依赖它"),

        new ExternalToolInfo(
            Type: ToolType.GARbro,
            DisplayName: "GARbro Console",
            ExecutableName: "GARbro.Console.exe",
            RelativeInstallDir: "tools/garbro/",
            DownloadUrl: "https://github.com/nanami5270/GARbro-Mod",
            ExpectedVersionArgs: ["-l"],
            VersionPattern: @"Recognized resource formats",
            Purpose: "游戏封包解包：XP3/ARC 等容器 → raw/scripts、images、voice 资产（源码单步编译，GameData/Formats.dat 须与 exe 同级）"),

        new ExternalToolInfo(
            Type: ToolType.FreeMote,
            DisplayName: "FreeMote Toolkit",
            ExecutableName: "PsbDecompile.exe",
            RelativeInstallDir: "tools/freemote/",
            DownloadUrl: "https://github.com/UlyssesWu/FreeMote/releases",
            ExpectedVersionArgs: ["-h"],
            VersionPattern: @"FreeMote PSB Decompiler",
            Sha256: "b36f5de80458391a4fdca8458152114f7c22b8a627fd8bc80cce964339ffc99e",
            Purpose: "PSB/E-mote 模型双向反编译与重构（PsBuild.exe 同目录共存，供回编译）"),

        new ExternalToolInfo(
            Type: ToolType.XDelta,
            DisplayName: "XDelta3",
            ExecutableName: "xdelta3.exe",
            RelativeInstallDir: "tools/xdelta/",
            DownloadUrl: "https://github.com/jmacd/xdelta-gpl/releases",
            ExpectedVersionArgs: [],
            VersionPattern: @"xdelta3\s+version\s+([0-9.]+)",
            Purpose: "增量差分补丁生成与应用（最终补丁交付的打包通道）"),

        new ExternalToolInfo(
            Type: ToolType.BGI,
            DisplayName: "BGI Hazuki Toolkit",
            ExecutableName: "BGI_Hazuki_TextTool.exe",
            RelativeInstallDir: "tools/bgi/",
            DownloadUrl: "https://github.com/ArisuMika520/BGI-Hazuki/releases",
            ExpectedVersionArgs: [],
            VersionPattern: @"BGI_Hazuki TextTool",
            Purpose: "BGI/Ethornell DSC 脚本双向：extract（dump）/ apply（回编译）；" +
                     "封包解包由同目录 BGI_Unpacker.exe（BURIKO ARC20，拖拽式交互）承担"),

        new ExternalToolInfo(
            Type: ToolType.CatSystem2,
            DisplayName: "CatSystem2 Tools",
            ExecutableName: "cs2_decompile.exe",
            RelativeInstallDir: "tools/cs2/",
            DownloadUrl: "https://github.com/Wolverator/CatSystem2-Simple-Translating-Tools",
            ExpectedVersionArgs: ["-h"],
            VersionPattern: @"CatSystem2 Script Decompiler",
            Purpose: "CatSystem2 CST/FES/ANM 反编译（-i/-o/-u|-hr|-md 实测）；" +
                     "辅助 CLI：mc.exe（官方 Message Compiler）、exkifint_v3.exe（int 解密，需配 game.exe）、hgx2bmp.exe（HG 图像）"),
    ];

    private static IReadOnlyDictionary<ToolType, ExternalToolInfo>? _catalog;

    /// <summary>按类型检索的注册表视图（探测器的主数据源）。</summary>
    public static IReadOnlyDictionary<ToolType, ExternalToolInfo> Catalog() =>
        _catalog ??= DefaultTools.ToDictionary(tool => tool.Type, tool => tool);

    public static ExternalToolInfo Get(ToolType type) =>
        Catalog().TryGetValue(type, out var info)
            ? info
            : throw new ToolchainException($"工具链注册表中没有 {type} 的元数据");
}
