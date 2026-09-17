using GalPipeline.Core.Toolchain;
using GalPipeline.Core.Translation;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 进程级翻译设置仓库的契约：默认值、快照原子替换、凭据归一化、
/// 作用域隔离，以及**工具链自定义路径映射**的持久化与快照合并。
/// Studio 批量翻译读取 <see cref="TranslationSettingsStore.Current"/> 组装
/// RPC 配置 —— 仓库语义错了，「设置页改了不生效」就会以另一种形式回来。
/// </summary>
[Collection(nameof(ProcessLevelStoreCollection))]
public class TranslationSettingsTests : IDisposable
{
    private readonly TranslationSettingsSnapshot _baseline;

    public TranslationSettingsTests()
    {
        _baseline = TranslationSettingsStore.Current; // 进程级状态：用完即还
    }

    public void Dispose() => TranslationSettingsStore.Update(_baseline);

    [Fact]
    public void Default_snapshot_points_to_local_loopback_mock()
    {
        var settings = TranslationSettingsStore.Current;

        // 离线红线：出厂默认必须是本地环回 Mock（零额度、零外网），
        // 而不是任何需要密钥的商业端点。
        Assert.Equal("http://127.0.0.1:18080/v1", settings.ApiBase);
        Assert.Equal("glm-4-flash", settings.ModelName);
        Assert.Equal(0.3, settings.Temperature);
        Assert.Null(settings.ReasoningEffort);
        Assert.Null(settings.ApiKey);
    }

    [Fact]
    public void Update_replaces_snapshot_atomically()
    {
        var before = TranslationSettingsStore.Current;
        try
        {
            var snapshot = new TranslationSettingsSnapshot(
                ApiBase: "https://api.example.com/v1",
                ApiKey: "sk-test",
                ModelName: "reasoning-x",
                Temperature: 0.9,
                ReasoningEffort: "high");
            TranslationSettingsStore.Update(snapshot);

            Assert.Same(snapshot, TranslationSettingsStore.Current);
            Assert.Equal("high", TranslationSettingsStore.Current.ReasoningEffort);
        }
        finally
        {
            TranslationSettingsStore.Update(before); // 还原进程级状态，避免污染其他测试
        }
    }

    [Fact]
    public void Update_rejects_null_snapshot()
    {
        Assert.Throws<ArgumentNullException>(() => TranslationSettingsStore.Update(null!));
    }

    [Fact]
    public void Snapshot_is_immutable_record()
    {
        var snapshot = new TranslationSettingsSnapshot(
            ApiBase: "http://x/v1", ApiKey: null, ModelName: "m", Temperature: 0.5,
            ReasoningEffort: null);

        // record 的 with 表达式产出新实例，原快照零篡改
        var adjusted = snapshot with { Temperature = 1.5 };
        Assert.Equal(0.5, snapshot.Temperature);
        Assert.Equal(1.5, adjusted.Temperature);
        Assert.NotSame(snapshot, adjusted);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void Normalize_key_treats_blank_as_anonymous(string? raw)
    {
        Assert.Null(TranslationSettingsStore.NormalizeKey(raw));
    }

    [Fact]
    public void Normalize_key_trims_real_key()
    {
        Assert.Equal("sk-1", TranslationSettingsStore.NormalizeKey(" sk-1 "));
    }

    // ------------------------------------------------------------------
    // 作用域通道：多工程/多标签页的配置隔离（DI 预留）
    // ------------------------------------------------------------------

    [Fact]
    public void BeginScope_overrides_current_and_dispose_restores_base()
    {
        var baseline = TranslationSettingsStore.Current;
        try
        {
            var overrideSnapshot = baseline with { ModelName = "scoped-model" };
            using (TranslationSettingsStore.BeginScope(overrideSnapshot))
            {
                Assert.Same(overrideSnapshot, TranslationSettingsStore.Current);
                Assert.Equal("scoped-model", TranslationSettingsStore.Current.ModelName);
            }

            Assert.Same(baseline, TranslationSettingsStore.Current);
        }
        finally
        {
            TranslationSettingsStore.Update(baseline);
        }
    }

    [Fact]
    public void Update_during_scope_writes_base_and_is_visible_after_dispose()
    {
        var scoped = _baseline with { Temperature = 1.25 };
        var rewritten = _baseline with { Temperature = 0.7 };
        using (TranslationSettingsStore.BeginScope(scoped))
        {
            TranslationSettingsStore.Update(rewritten);
            // 作用域内的读取仍见覆盖值：页面写入不得悄悄篡改别的工程正在用的配置
            Assert.Same(scoped, TranslationSettingsStore.Current);
        }

        Assert.Same(rewritten, TranslationSettingsStore.Current);
    }

    // ------------------------------------------------------------------
    // 工具链自定义路径：持久化、快照合并、事件防抖
    // ------------------------------------------------------------------

    [Fact]
    public void Set_custom_tool_path_persists_and_merges_with_other_fields()
    {
        // 前置：先改端点，再设工具路径 —— 两者必须共存于同一快照（合并非覆盖）
        TranslationSettingsStore.Update(_baseline with { ApiBase = "https://api.example.com/v1" });

        TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe");

        var current = TranslationSettingsStore.Current;
        Assert.Equal("https://api.example.com/v1", current.ApiBase); // 其他字段不被工具路径写入破坏
        Assert.Equal(@"D:\tools\ffmpeg.exe", current.CustomToolPaths![ToolType.FFmpeg]);
        Assert.Equal(@"D:\tools\ffmpeg.exe", TranslationSettingsStore.GetCustomToolPath(ToolType.FFmpeg));
    }

    [Fact]
    public void Clearing_custom_tool_path_removes_entry_and_falls_back_to_waterfall()
    {
        TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe");
        TranslationSettingsStore.SetCustomToolPath(ToolType.GARbro, @"D:\tools\gar\GARbro.exe");

        TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, null); // 清除 = 删条目

        var paths = TranslationSettingsStore.Current.CustomToolPaths!;
        Assert.False(paths.ContainsKey(ToolType.FFmpeg)); // 回退瀑布流的前提：条目彻底消失
        Assert.True(paths.ContainsKey(ToolType.GARbro)); // 其他工具的映射不受牵连
        Assert.Null(TranslationSettingsStore.GetCustomToolPath(ToolType.FFmpeg));
    }

    [Fact]
    public void Blank_path_is_normalized_to_clear()
    {
        TranslationSettingsStore.SetCustomToolPath(ToolType.XDelta, @"D:\x\xdelta3.exe");

        // 空白与 null 同语义：清除（设置页的文本框可能送来空串）
        TranslationSettingsStore.SetCustomToolPath(ToolType.XDelta, "   ");
        Assert.Null(TranslationSettingsStore.GetCustomToolPath(ToolType.XDelta));

        TranslationSettingsStore.SetCustomToolPath(ToolType.XDelta, @"D:\x\xdelta3.exe");
        TranslationSettingsStore.SetCustomToolPath(ToolType.XDelta, "");
        Assert.Null(TranslationSettingsStore.GetCustomToolPath(ToolType.XDelta));
    }

    [Fact]
    public void Set_custom_tool_path_trims_value()
    {
        TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @" D:\tools\ffmpeg.exe ");

        Assert.Equal(@"D:\tools\ffmpeg.exe", TranslationSettingsStore.GetCustomToolPath(ToolType.FFmpeg));
    }

    [Fact]
    public void Toolchain_changed_fires_only_on_actual_content_change()
    {
        var fired = 0;
        void Handler() => fired++;
        TranslationSettingsStore.ToolchainChanged += Handler;
        try
        {
            TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe");
            var afterFirst = fired;

            TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\tools\ffmpeg.exe"); // 相同值：防抖
            TranslationSettingsStore.SetCustomToolPath(ToolType.GARbro, "  "); // 清除从未设置的条目：防抖

            Assert.Equal(1, afterFirst);
            Assert.Equal(1, fired); // 后两次零事件 —— 缓存失效与重建不做无用功
        }
        finally
        {
            TranslationSettingsStore.ToolchainChanged -= Handler;
        }
    }

    [Fact]
    public void Update_with_snapshot_carries_dict_reference_and_replaces_mapping()
    {
        TranslationSettingsStore.SetCustomToolPath(ToolType.FFmpeg, @"D:\old.exe");
        TranslationSettingsStore.SetCustomToolPath(ToolType.GARbro, @"D:\g.exe");

        // 整体替换语义：新快照携带的映射整体生效（Settings 页 PushToStore
        // 必须自己携带旧映射 —— 由绑定契约测试钉住，这里验证替换本身）
        var replacement = new TranslationSettingsSnapshot(
            ApiBase: "http://127.0.0.1:18080/v1",
            ApiKey: null,
            ModelName: "glm-4-flash",
            Temperature: 0.3,
            ReasoningEffort: null,
            CustomToolPaths: new Dictionary<ToolType, string> { [ToolType.FreeMote] = @"D:\fm.exe" });
        TranslationSettingsStore.Update(replacement);

        var paths = TranslationSettingsStore.Current.CustomToolPaths!;
        Assert.False(paths.ContainsKey(ToolType.FFmpeg));
        Assert.False(paths.ContainsKey(ToolType.GARbro));
        Assert.Equal(@"D:\fm.exe", paths[ToolType.FreeMote]);
    }

    [Fact]
    public void Tool_paths_equal_compares_by_content_not_reference()
    {
        var a = new Dictionary<ToolType, string> { [ToolType.FFmpeg] = @"D:\f.exe" };
        var b = new Dictionary<ToolType, string> { [ToolType.FFmpeg] = @"D:\f.exe" };

        Assert.True(TranslationSettingsSnapshot.ToolPathsEqual(a, b)); // 不同实例、同内容
        Assert.True(TranslationSettingsSnapshot.ToolPathsEqual(null, new Dictionary<ToolType, string>()));
        Assert.False(TranslationSettingsSnapshot.ToolPathsEqual(a, null));
        Assert.False(TranslationSettingsSnapshot.ToolPathsEqual(
            a, new Dictionary<ToolType, string> { [ToolType.FFmpeg] = @"D:\other.exe" }));
    }

    [Fact]
    public void With_custom_tool_path_is_immutable_merge()
    {
        var snapshot = _baseline;
        var withTool = snapshot.WithCustomToolPath(ToolType.FFmpeg, @"D:\f.exe");
        var cleared = withTool.WithCustomToolPath(ToolType.FFmpeg, null);

        Assert.Null(snapshot.CustomToolPaths); // 原快照零篡改
        Assert.Equal(@"D:\f.exe", withTool.CustomToolPaths![ToolType.FFmpeg]);
        Assert.Empty(cleared.CustomToolPaths!);
    }
}
