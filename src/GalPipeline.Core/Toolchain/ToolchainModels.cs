namespace GalPipeline.Core.Toolchain;

/// <summary>外部工具链资产类型（新增工具 = 扩展枚举 + 注册表登记）。</summary>
public enum ToolType
{
    FFmpeg,
    GARbro,
    FreeMote,
    XDelta,
    BGI,
    CatSystem2,
}

/// <summary>
/// 单个外部工具的**静态元数据**：它是谁、叫什么、装在哪、去哪下、怎么验。
/// 解析器据此执行四级瀑布流探测，下载器据此执行一键装配 —— 两边消费同一份事实。
/// </summary>
/// <param name="Type">工具类型（注册表主键）。</param>
/// <param name="DisplayName">界面可读名（异常引导与设置页展示用）。</param>
/// <param name="ExecutableName">可执行文件名（如 ffmpeg.exe；探测按此名匹配）。</param>
/// <param name="RelativeInstallDir">本地工具箱内的相对安装目录（约定 tools/{tool}/）。</param>
/// <param name="DownloadUrl">官方发布页 / 直接下载地址（Missing 引导与一键下载器共用）。</param>
/// <param name="ExpectedVersionArgs">健康检查（ProbeVersion）的命令行参数。</param>
/// <param name="VersionPattern">从探测输出提取版本号的正则（首捕获组为版本）。</param>
/// <param name="Sha256">发布压缩包的校验和（一键下载器完整性验证用；未知时 null）。</param>
/// <param name="IsMandatory">是否为流水线硬依赖（Missing 时阻断相关功能并在 UI 醒目提示）。</param>
/// <param name="Purpose">一句话用途描述（设置页工具链卡片直接展示）。</param>
public sealed record ExternalToolInfo(
    ToolType Type,
    string DisplayName,
    string ExecutableName,
    string RelativeInstallDir,
    string DownloadUrl,
    IReadOnlyList<string> ExpectedVersionArgs,
    string VersionPattern,
    string? Sha256 = null,
    bool IsMandatory = false,
    string Purpose = "")
{
    /// <summary>
    /// 本地工具箱内的相对路径约定：{toolboxRoot}/{RelativeInstallDir}/{ExecutableName}。
    /// 元数据允许 URL 风格分隔符（"tools/ffmpeg/"），此处统一归一为平台分隔符，
    /// 杜绝混合分隔符在断言与显示上的漂移。
    /// </summary>
    public string ToolboxRelativePath
    {
        get
        {
            var normalized = RelativeInstallDir.Replace('/', Path.DirectorySeparatorChar)
                .Replace('\\', Path.DirectorySeparatorChar);
            return Path.Combine(normalized, ExecutableName);
        }
    }
}

/// <summary>健康检查结论的四态：可用 / 未装配 / 损坏（无法启动或退出码异常）/ 版本不符。</summary>
public enum ToolStatusKind
{
    Ready,
    Missing,
    Corrupted,
    InvalidVersion,
}

/// <summary>命中来源（四级瀑布流的层级回执，供审计与设置页展示）。</summary>
public enum ToolOrigin
{
    None,
    CustomOverride,
    LocalToolbox,
    PathEnvironment,
}

/// <summary>
/// 探测结论（不可变快照）：<see cref="Kind"/> 为 Ready 时携带
/// **已实测版本号**与绝对路径；其余状态携带引导性 Detail。
/// </summary>
public sealed record ToolStatus(
    ToolStatusKind Kind,
    ToolType Type,
    string? Path = null,
    string? Version = null,
    string? Detail = null,
    ToolOrigin Origin = ToolOrigin.None)
{
    /// <summary>Ready 且路径可执行（消费方据此直接起子进程）。</summary>
    public bool IsReady => Kind is ToolStatusKind.Ready && !string.IsNullOrWhiteSpace(Path);
}
