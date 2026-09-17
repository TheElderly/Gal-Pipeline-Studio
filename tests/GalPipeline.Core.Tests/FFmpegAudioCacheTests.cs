using System.Diagnostics;
using GalPipeline.Core.Audio;
using GalPipeline.Core.Toolchain;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// FFmpeg 转码缓存服务的无头契约测试。
///
/// 本机可能未装配 FFmpeg —— 进程生命周期与缓存命中的验证通过两条缝完成：
/// **进程缝**（子类把 ffmpeg 替换成 cmd.exe 脚本，同一条
/// StartProcess → Kill(entireProcessTree) → WaitForExit 链路全部实跑）
/// 与**工具链缝**（注入 IToolResolver 桩，Missing / Ready 两态可控）。
/// 真实的四级瀑布流探测由 ToolchainTests 单独覆盖。
/// </summary>
public class FFmpegAudioCacheTests : IDisposable
{
    private readonly string _root;

    public FFmpegAudioCacheTests()
    {
        _root = Path.Combine(Path.GetTempPath(), "galpipeline-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_root);
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_root, recursive: true);
        }
        catch
        {
            // 缓存目录被并发句柄占用时交给系统临时目录清理，不影响断言
        }
    }

    /// <summary>工具链桩：Ready 时携带一个假路径（进程缝会替换实际启动的进程）。</summary>
    private sealed class StubToolResolver : IToolResolver
    {
        public StubToolResolver(ToolStatus status) => Status = status;

        public ToolStatus Status { get; set; }

        public ToolStatus Resolve(ToolType type) => Status with { Type = type };
    }

    private static ToolStatus ReadyStub => new(
        ToolStatusKind.Ready, ToolType.FFmpeg,
        Path: @"C:\toolchain-stub\ffmpeg.exe", Version: "stub-1.0",
        Origin: ToolOrigin.LocalToolbox);

    private string CreateSource(string name = "vo_00012.ogg")
    {
        var path = Path.Combine(_root, name);
        File.WriteAllBytes(path, [0x4F, 0x67, 0x67, 0x53]); // "OggS"
        return path;
    }

    // ------------------------------------------------------------------
    // 1. 哈希命名：确定性 + 内容敏感性
    // ------------------------------------------------------------------

    [Fact]
    public void Cache_path_is_deterministic_for_unchanged_source()
    {
        var cache = new FFmpegAudioCache(_root);
        var source = CreateSource();

        Assert.Equal(cache.CachePathFor(source), cache.CachePathFor(source));
        Assert.StartsWith(cache.CacheDirectory, cache.CachePathFor(source));
        Assert.EndsWith(".wav", cache.CachePathFor(source));
    }

    [Fact]
    public void Cache_path_changes_when_source_is_modified()
    {
        var cache = new FFmpegAudioCache(_root);
        var source = CreateSource();
        var before = cache.CachePathFor(source);

        File.WriteAllBytes(source, [0x4F, 0x67, 0x67, 0x53, 0x01]);
        File.SetLastWriteTimeUtc(source, File.GetLastWriteTimeUtc(source).AddMinutes(1));

        Assert.NotEqual(before, cache.CachePathFor(source));
    }

    [Fact]
    public void Cache_path_differs_between_identical_named_files_in_different_dirs()
    {
        var cache = new FFmpegAudioCache(_root);
        var sub = Path.Combine(_root, "disc2");
        Directory.CreateDirectory(sub);
        var a = CreateSource();
        var b = Path.Combine(sub, "vo_00012.ogg");
        File.WriteAllBytes(b, [0x4F, 0x67, 0x67, 0x53]);

        Assert.NotEqual(cache.CachePathFor(a), cache.CachePathFor(b));
    }

    // ------------------------------------------------------------------
    // 2. 失败面：源缺失 / FFmpeg 未装配 —— 诚实文案，绝不假装成功
    // ------------------------------------------------------------------

    [Fact]
    public async Task Missing_source_fails_without_spawning_anything()
    {
        var cache = new FakeFfmpegCache(Path.Combine(_root, "c1"), FakeMode.Ok);
        var outcome = await cache.GetPlayableAsync(
            Path.Combine(_root, "ghost.ogg"), CancellationToken.None);

        Assert.Null(outcome.PlayablePath);
        Assert.Contains("不存在", outcome.FailureReason);
        Assert.Empty(cache.Spawned);
    }

    [Fact]
    public async Task Missing_toolchain_throws_guided_exception()
    {
        var cache = new FFmpegAudioCache(
            Path.Combine(_root, "c2"),
            new StubToolResolver(new ToolStatus(
                ToolStatusKind.Missing, ToolType.FFmpeg, Detail: "四级瀑布流未命中")));
        var source = CreateSource();

        var exception = await Assert.ThrowsAsync<ToolchainMissingException>(
            () => cache.GetPlayableAsync(source, CancellationToken.None));

        // 引导上下文必须可直接行动：工具名 / 工具箱落位（归一化分隔符）/ 官方下载
        Assert.Equal(ToolType.FFmpeg, exception.ToolType);
        Assert.Contains("FFmpeg", exception.Message);
        Assert.Contains("ffmpeg.exe", exception.Message);
        Assert.Contains("http", exception.Message);
    }

    // ------------------------------------------------------------------
    // 3. 进程缝：转码成功 → 缓存命中短路；取消 → 子进程被击杀
    // ------------------------------------------------------------------

    private sealed class FakeFfmpegCache : FFmpegAudioCache
    {
        public FakeFfmpegCache(string cacheDirectory, FakeMode mode)
            : base(cacheDirectory, new StubToolResolver(ReadyStub)) => Mode = mode;

        public FakeMode Mode { get; }

        public List<Process> Spawned { get; } = new();

        public List<int> SpawnedPids { get; } = new();

        protected override Process? StartProcess(ProcessStartInfo startInfo)
        {
            // startInfo 的最后一个参数是 .part 产物路径；按模式替换为 cmd 脚本
            var target = startInfo.ArgumentList[^1];
            var psi = new ProcessStartInfo("cmd.exe")
            {
                CreateNoWindow = true,
                UseShellExecute = false,
            };
            psi.ArgumentList.Add("/d");
            psi.ArgumentList.Add("/s");
            psi.ArgumentList.Add("/c");
            psi.ArgumentList.Add(Mode == FakeMode.Ok
                ? $"type NUL > {target}"      // 测试路径无空格：避免 cmd 的嵌套引号解析
                : "ping -n 60 127.0.0.1");
            var process = Process.Start(psi)!;
            Spawned.Add(process);
            SpawnedPids.Add(process.Id);
            return process;
        }

        /// <summary>按 PID 新建只读句柄查存活（服务会 Dispose 其持有对象，不能复用）。</summary>
        public bool IsProcessAlive(int pid)
        {
            try
            {
                return !Process.GetProcessById(pid).HasExited;
            }
            catch (ArgumentException)
            {
                return false; // PID 已被系统回收 = 进程彻底退出
            }
        }
    }

    private enum FakeMode
    {
        Ok,
        Hang,
    }

    [Fact]
    public async Task Transcode_writes_cached_wav_and_second_call_short_circuits()
    {
        var cache = new FakeFfmpegCache(Path.Combine(_root, "c3"), FakeMode.Ok);
        var source = CreateSource();

        var first = await cache.GetPlayableAsync(source, CancellationToken.None);

        Assert.Null(first.FailureReason);
        Assert.NotNull(first.PlayablePath);
        Assert.True(File.Exists(first.PlayablePath), "转码产物必须真实落盘");
        Assert.EndsWith(".wav", first.PlayablePath);
        Assert.False(File.Exists(first.PlayablePath + ".part"), ".part 必须已原子改名");
        Assert.Single(cache.Spawned);

        var second = await cache.GetPlayableAsync(source, CancellationToken.None);
        Assert.Equal(first.PlayablePath, second.PlayablePath);
        Assert.Single(cache.Spawned); // 缓存命中：零进程开销
        Assert.True(cache.HasFreshCache(source));
    }

    [Fact]
    public async Task Cancellation_kills_the_inflight_transcode_process()
    {
        var cache = new FakeFfmpegCache(Path.Combine(_root, "c4"), FakeMode.Hang);
        var source = CreateSource();
        using var cts = new CancellationTokenSource(400);

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => cache.GetPlayableAsync(source, cts.Token));

        // 击杀必须落地：轮询 PID 存活态直至退出（服务会 Dispose 其 Process 对象，
        // 不能复用那个实例的句柄 —— 用 PID 新建只读句柄验证「无僵尸」）
        var pid = Assert.Single(cache.SpawnedPids);
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (cache.IsProcessAlive(pid) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(50);
        }
        Assert.False(cache.IsProcessAlive(pid), "取消后子进程必须被终止");
        Assert.False(cache.HasFreshCache(source), "被取消的转码不得留下可命中的缓存");
    }

    [Fact]
    public async Task Explicit_cancel_before_start_throws_immediately()
    {
        var cache = new FakeFfmpegCache(Path.Combine(_root, "c5"), FakeMode.Ok);
        var cts = new CancellationTokenSource();
        cts.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => cache.GetPlayableAsync(CreateSource(), cts.Token));
        Assert.Empty(cache.Spawned);
    }
}
