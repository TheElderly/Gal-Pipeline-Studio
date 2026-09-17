using GalPipeline.Core.Toolchain;
using GalPipeline.Core.Translation;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 工具链运行时的联动契约：设置仓库的映射变更必须**即时**反映到解析器
/// —— 这是「设置页改了路径、转码/解包仍走旧工具」这一软花架子的防线。
/// </summary>
[Collection(nameof(ProcessLevelStoreCollection))]
public class ToolchainRuntimeTests : IDisposable
{
    private readonly TranslationSettingsSnapshot _baseline;

    public ToolchainRuntimeTests() => _baseline = TranslationSettingsStore.Current;

    public void Dispose() => TranslationSettingsStore.Update(_baseline);

    [Fact]
    public void Current_is_available_and_changes_after_mapping_update()
    {
        var before = ToolchainRuntime.Current;

        // 映射变更（内容实际变化）→ 解析器实例整体替换
        TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe");
        var after = ToolchainRuntime.Current;

        Assert.NotSame(before, after);
    }

    [Fact]
    public void Resolver_changed_event_fires_on_mapping_change()
    {
        var fired = 0;
        void Handler() => fired++;
        ToolchainRuntime.ResolverChanged += Handler;
        try
        {
            TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe");
            var afterFirst = fired;

            TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe"); // 无变化：防抖

            Assert.Equal(1, afterFirst);
            Assert.Equal(1, fired);
        }
        finally
        {
            ToolchainRuntime.ResolverChanged -= Handler;
        }
    }

    [Fact]
    public void Build_resolver_injects_custom_paths_into_waterfall()
    {
        // 快照映射 → 解析器构造参数的通道：自定义路径必须是瀑布流的最高优先级。
        // 探测底物 = cmd.exe 副本（真实存在、快速返回）—— 断言的是「命中了
        // Custom 层」这一路由事实，健康检查结论（版本匹配与否）由 ToolchainTests 承载。
        var exe = Path.Combine(Path.GetTempPath(), "galpipeline-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(exe);
        var probe = Path.Combine(exe, "probe.cmd");
        File.Copy(Path.Combine(Environment.SystemDirectory, "cmd.exe"), probe);

        var snapshot = new TranslationSettingsSnapshot(
            ApiBase: "http://127.0.0.1:18080/v1",
            ApiKey: null,
            ModelName: "glm-4-flash",
            Temperature: 0.3,
            ReasoningEffort: null,
            CustomToolPaths: new Dictionary<ToolType, string> { [ToolType.GARbro] = probe });
        var resolver = ToolchainRuntime.BuildResolver(snapshot);

        var status = resolver.Resolve(ToolType.GARbro);

        Assert.Equal(ToolOrigin.CustomOverride, status.Origin); // 命中来源必须是 Custom 层
        Assert.Equal(probe, status.Path);
        try { Directory.Delete(exe, recursive: true); } catch { /* 探测句柄短暂占用 */ }
    }

    [Fact]
    public void Empty_mapping_falls_back_to_plain_waterfall()
    {
        var snapshot = TranslationSettingsStore.Current with { CustomToolPaths = null };
        var resolver = ToolchainRuntime.BuildResolver(snapshot);

        // 无映射时与默认构造等价：四级瀑布流正常执行并给出四态结论之一
        // （本机装配了 FFmpeg 就是 Ready，没装就是 Missing —— 断言的是「不抛」）
        var status = resolver.Resolve(ToolType.FFmpeg);
        Assert.True(Enum.IsDefined(status.Kind));
    }

    [Fact]
    public void Refresh_rebuilds_even_without_mapping_change()
    {
        var before = ToolchainRuntime.Current;

        ToolchainRuntime.Refresh(); // 设置页「重新探测」的强制重建入口

        Assert.NotSame(before, ToolchainRuntime.Current);
    }
}
