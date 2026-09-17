namespace GalPipeline.Core.Toolchain;

/// <summary>
/// 工具解析器：给定 ToolType，产出该工具「现在、本机」的真实可用状态。
/// 实现方必须执行四级瀑布流（自定义路径 → 本地工具箱 → PATH → Missing），
/// 且**任何分支都不得抛异常** —— Missing 是合法结论，不是错误。
/// </summary>
public interface IToolResolver
{
    ToolStatus Resolve(ToolType type);
}

/// <summary>
/// 一键装配器契约（在线拉取的**预留抽象**，本切片只立契约与测试桩）：
/// 下载 → 扁平化解压 → SHA-256 完整性校验，三步均为独立可测单元。
/// </summary>
public interface IToolchainDownloader
{
    /// <summary>
    /// 下载工具发布压缩包到 destinationDirectory，返回压缩包绝对路径。
    /// <paramref name="progress"/> 回传 ∈ [0,1]（保证单调且以 1 收尾）。
    /// </summary>
    Task<string> DownloadAsync(
        ExternalToolInfo tool,
        string destinationDirectory,
        IProgress<double>? progress = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// 扁平化解压：溶解压缩包顶层目录（如 ffmpeg-7.1-essentials_build/），
    /// 使 {destinationDirectory}/{ExecutableName} 直接可达，返回产物目录。
    /// </summary>
    Task<string> ExtractFlattenedAsync(
        string archivePath,
        string destinationDirectory,
        CancellationToken cancellationToken = default);

    /// <summary>SHA-256 完整性校验（对比 tool.Sha256）；不符即抛 ToolchainException。</summary>
    Task VerifyChecksumAsync(
        string archivePath,
        ExternalToolInfo tool,
        CancellationToken cancellationToken = default);
}
