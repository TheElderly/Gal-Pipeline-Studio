namespace GalPipeline.Core.Toolchain;

using GalPipeline.Core.Translation;

/// <summary>
/// 进程级工具链解析器运行时 —— 外部工具路径设置的**动态消费出口**。
///
/// 为什么需要它：<see cref="ExternalToolResolver"/> 是无状态的（每次
/// Resolve 现查现探），但它的构造参数（自定义路径映射）来自设置仓库 ——
/// 用户在设置页改了路径，持有旧 resolver 实例的服务（FFmpeg 转码缓存、
/// GARbro 解包）必须能拿到**重建后的**实例，否则「改了不生效」的软花架子
/// 会在工具链层复发。
///
/// 联动链路：设置页 <c>SetCustomToolPath</c> → 仓库 <c>ToolchainChanged</c>
/// → 本类重建 <see cref="ExternalToolResolver"/>（携带最新映射）并触发
/// <c>ResolverChanged</c> → 消费方在**下一次调用**时通过 <see cref="Current"/>
/// 拿到新实例（缓存了探测结果的 FFmpegAudioCache 同时收到失效通知）。
///
/// 作用域语义：快照合并沿用仓库口径 —— <c>SetCustomToolPath</c> 恒写基准，
/// 作用域覆盖值不含工具链映射（页面写入不越权篡改其他工程正在使用的
/// 覆盖值），故运行时只依赖 <c>TranslationSettingsStore.ToolchainChanged</c>
/// 与基准快照，天然无作用序歧义。
/// </summary>
public static class ToolchainRuntime
{
    private static readonly object Gate = new();
    private static IToolResolver _current = Build(TranslationSettingsStore.Current);

    /// <summary>
    /// 工具链映射变更后的失效通知：缓存了「解析结果」的消费方（如
    /// FFmpegAudioCache 的 <c>_resolvedFfmpegPath</c>）必须据此清缓存，
    /// 让下一次调用重新走四级瀑布流。
    /// </summary>
    public static event Action? ResolverChanged;

    /// <summary>当前生效的解析器（实例在映射变更时整体替换，消费方每次调用现取）。</summary>
    public static IToolResolver Current
    {
        get
        {
            lock (Gate)
            {
                return _current;
            }
        }
    }

    static ToolchainRuntime()
    {
        TranslationSettingsStore.ToolchainChanged += Refresh;
    }

    /// <summary>强制重建解析器（设置页「重新探测」与仓库变更共用入口）。</summary>
    public static void Refresh()
    {
        lock (Gate)
        {
            _current = Build(TranslationSettingsStore.Current);
        }
        ResolverChanged?.Invoke();
    }

    /// <summary>按快照组装解析器（纯函数，供测试直接验证映射注入）。</summary>
    public static ExternalToolResolver BuildResolver(TranslationSettingsSnapshot snapshot) =>
        Build(snapshot);

    private static ExternalToolResolver Build(TranslationSettingsSnapshot snapshot) =>
        snapshot.CustomToolPaths is { Count: > 0 } paths
            ? new ExternalToolResolver(customPaths: paths)
            : new ExternalToolResolver();
}
