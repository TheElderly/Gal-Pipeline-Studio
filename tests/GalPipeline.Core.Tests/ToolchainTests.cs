using GalPipeline.Core.Toolchain;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 统一工具链基础设施的契约测试：四级瀑布流优先级、版本健康检查、
/// 注册表元数据完备性、下载器契约桩。
///
/// 探测底物策略：以 **cmd.exe 的副本**充当被探测工具（重命名为
/// probe-tool.exe），用 ``/d /s /c ver`` 做版本探测 —— 输出稳定、
/// 无需 FFmpeg 在场、四级优先级的每一层都用真实文件实跑。
/// </summary>
public class ToolchainTests : IDisposable
{
    private readonly string _root;

    public ToolchainTests()
    {
        _root = Path.Combine(Path.GetTempPath(), "galpipeline-toolchain", Guid.NewGuid().ToString("N"));
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
            // 被探测进程句柄短暂占用时交给系统清理，不影响断言
        }
    }

    /// <summary>
    /// 测试专用工具元数据：探测底物 = cmd.exe 的**改名副本**。
    /// 刻意用 ``echo Version 9.9.9`` 而非 ``ver`` —— 改名后的 cmd 内建
    /// ``ver`` 依赖消息表解析会失败（实测），``echo`` 与输出完全确定。
    /// </summary>
    private static ExternalToolInfo ProbeToolInfo => new(
        Type: ToolType.GARbro, // 复用注册表类型位；目录/名称完全由本测试自定义
        DisplayName: "Probe Tool",
        ExecutableName: "probe-tool.exe",
        RelativeInstallDir: "tools/testtool/",
        DownloadUrl: "https://example.invalid/download",
        ExpectedVersionArgs: ["/d", "/s", "/c", "echo Version 9.9.9"],
        VersionPattern: @"Version\s+([0-9.]+)");

    private string CopyCmdAs(string relativePath)
    {
        var path = Path.Combine(_root, relativePath);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.Copy(Path.Combine(Environment.SystemDirectory, "cmd.exe"), path);
        return Path.GetFullPath(path); // 归一化分隔符，与 resolver 产出的路径同构
    }

    private static IReadOnlyDictionary<ToolType, ExternalToolInfo> CatalogOf(ExternalToolInfo info) =>
        new Dictionary<ToolType, ExternalToolInfo> { [info.Type] = info };

    // ------------------------------------------------------------------
    // 1. 四级瀑布流：严格优先级
    // ------------------------------------------------------------------

    [Fact]
    public async Task Custom_override_beats_toolbox_beats_path()
    {
        var custom = CopyCmdAs("custom/probe-tool.exe");
        var toolbox = CopyCmdAs("toolbox/tools/testtool/probe-tool.exe");
        var pathDir = Path.Combine(_root, "onpath");
        CopyCmdAs("onpath/probe-tool.exe");
        var info = ProbeToolInfo;

        // 三层齐备 → 自定义胜出
        var resolver = new ExternalToolResolver(
            CatalogOf(info),
            customPaths: new Dictionary<ToolType, string> { [info.Type] = custom },
            toolboxRoot: Path.Combine(_root, "toolbox"),
            searchPaths: [pathDir]);
        var status = resolver.Resolve(info.Type);
        Assert.Equal(ToolStatusKind.Ready, status.Kind);
        Assert.Equal(ToolOrigin.CustomOverride, status.Origin);
        Assert.Equal(custom, status.Path);
        Assert.False(string.IsNullOrWhiteSpace(status.Version));

        // 撤掉自定义 → 工具箱胜出
        resolver = new ExternalToolResolver(
            CatalogOf(info),
            toolboxRoot: Path.Combine(_root, "toolbox"),
            searchPaths: [pathDir]);
        status = resolver.Resolve(info.Type);
        Assert.Equal(ToolOrigin.LocalToolbox, status.Origin);
        Assert.Equal(toolbox, status.Path);

        // 再撤掉工具箱 → PATH 胜出
        resolver = new ExternalToolResolver(
            CatalogOf(info),
            toolboxRoot: Path.Combine(_root, "nowhere"),
            searchPaths: [pathDir]);
        status = resolver.Resolve(info.Type);
        Assert.Equal(ToolOrigin.PathEnvironment, status.Origin);
        Assert.Contains("onpath", status.Path);
    }

    [Fact]
    public async Task Stale_custom_path_falls_through_to_toolbox()
    {
        var toolbox = CopyCmdAs("toolbox/tools/testtool/probe-tool.exe");
        var resolver = new ExternalToolResolver(
            CatalogOf(ProbeToolInfo),
            customPaths: new Dictionary<ToolType, string>
            {
                [ToolType.GARbro] = Path.Combine(_root, "ghost", "probe-tool.exe"),
            },
            toolboxRoot: Path.Combine(_root, "toolbox"),
            searchPaths: []);

        var status = resolver.Resolve(ToolType.GARbro);

        // 失效的自定义配置**继续瀑布**而不是判死：旧配置不该挡住现成的可用副本
        Assert.Equal(ToolStatusKind.Ready, status.Kind);
        Assert.Equal(ToolOrigin.LocalToolbox, status.Origin);
        Assert.Equal(toolbox, status.Path);
    }

    [Fact]
    public async Task Nothing_found_is_missing_and_never_throws()
    {
        var resolver = new ExternalToolResolver(
            CatalogOf(ProbeToolInfo),
            toolboxRoot: Path.Combine(_root, "empty"),
            searchPaths: []);

        var status = resolver.Resolve(ToolType.GARbro);

        Assert.Equal(ToolStatusKind.Missing, status.Kind);
        Assert.Null(status.Path);
        Assert.Contains("probe-tool.exe", status.Detail);
    }

    // ------------------------------------------------------------------
    // 2. 健康检查：版本正则 / 非法版本 / 损坏
    // ------------------------------------------------------------------

    [Fact]
    public void Probe_extracts_version_via_pattern()
    {
        CopyCmdAs("tools/testtool/probe-tool.exe");
        var resolver = new ExternalToolResolver(
            CatalogOf(ProbeToolInfo), toolboxRoot: _root);

        var status = resolver.Resolve(ToolType.GARbro);

        Assert.Equal(ToolStatusKind.Ready, status.Kind);
        Assert.Equal("9.9.9", status.Version); // echo 输出的确定性版本串
    }

    [Fact]
    public void Version_mismatch_is_invalid_version_not_missing()
    {
        var exe = CopyCmdAs("tools/testtool/probe-tool.exe");
        var info = ProbeToolInfo with { VersionPattern = @"ZZZNOMATCH\s+([0-9.]+)" };
        var resolver = new ExternalToolResolver(CatalogOf(info), toolboxRoot: _root);

        var status = resolver.Resolve(ToolType.GARbro);

        Assert.Equal(ToolStatusKind.InvalidVersion, status.Kind);
        Assert.Equal(exe, status.Path); // 文件在、但自称身份对不上
        Assert.Contains("版本号", status.Detail);
    }

    [Fact]
    public void Nonzero_probe_exit_is_corrupted()
    {
        var exe = CopyCmdAs("tools/testtool/probe-tool.exe");
        // cmd /c exit 3 → 探测命令非零退出 → 损坏
        var info = ProbeToolInfo with
        {
            ExpectedVersionArgs = ["/d", "/s", "/c", "exit 3"],
        };
        var resolver = new ExternalToolResolver(CatalogOf(info), toolboxRoot: _root);

        var status = resolver.Resolve(ToolType.GARbro);

        Assert.Equal(ToolStatusKind.Corrupted, status.Kind);
        Assert.Equal(exe, status.Path);
        Assert.Contains("退出码 3", status.Detail);
    }

    // ------------------------------------------------------------------
    // 3. 注册表元数据完备性
    // ------------------------------------------------------------------

    [Fact]
    public void Default_registry_covers_all_tools_with_valid_metadata()
    {
        var tools = ToolchainRegistry.DefaultTools;

        Assert.Equal(6, tools.Count);
        Assert.Equal(
            tools.Select(t => t.Type).Order().ToList(),
            tools.Select(t => t.Type).Distinct().Order().ToList());

        var ffmpeg = tools.Single(t => t.Type == ToolType.FFmpeg);
        Assert.True(ffmpeg.IsMandatory, "FFmpeg 是多媒体唯一底层，必须标记为硬依赖");
        Assert.Equal("ffmpeg.exe", ffmpeg.ExecutableName);
        Assert.Equal(@"ffmpeg\s+version\s+(\S+)", ffmpeg.VersionPattern);

        foreach (var tool in tools)
        {
            Assert.StartsWith("tools/", tool.RelativeInstallDir);
            Assert.EndsWith("/", tool.RelativeInstallDir);
            Assert.StartsWith("http", tool.DownloadUrl);
            // VersionPattern 必须是可编译的合法正则（防手滑写坏整条探测链）
            _ = new System.Text.RegularExpressions.Regex(
                tool.VersionPattern,
                System.Text.RegularExpressions.RegexOptions.IgnoreCase,
                TimeSpan.FromMilliseconds(500));
        }
    }

    // ------------------------------------------------------------------
    // 4. 下载器契约桩：进度回传 + SHA-256 完整性
    // ------------------------------------------------------------------

    /// <summary>同步记录的进度探针（Progress&lt;T&gt; 走线程池，断言会竞态）。</summary>
    private sealed class RecordingProgress : IProgress<double>
    {
        public List<double> Values { get; } = new();
        public void Report(double value) => Values.Add(value);
    }

    private sealed class StubDownloader : IToolchainDownloader
    {
        public int DownloadCalls { get; private set; }
        public int VerifyCalls { get; private set; }
        public string Content { get; set; } = "payload";

        public Task<string> DownloadAsync(
            ExternalToolInfo tool, string destinationDirectory,
            IProgress<double>? progress = null, CancellationToken cancellationToken = default)
        {
            DownloadCalls++;
            Directory.CreateDirectory(destinationDirectory);
            var archive = Path.Combine(destinationDirectory, $"{tool.ExecutableName}.zip");
            File.WriteAllText(archive, Content);
            progress?.Report(0.25);
            progress?.Report(0.75);
            progress?.Report(1.0);
            return Task.FromResult(archive);
        }

        public Task<string> ExtractFlattenedAsync(
            string archivePath, string destinationDirectory,
            CancellationToken cancellationToken = default)
        {
            Directory.CreateDirectory(destinationDirectory);
            var target = Path.Combine(destinationDirectory, "probe-tool.exe");
            File.WriteAllText(target, Content); // 扁平化：可执行文件直接可达
            return Task.FromResult(destinationDirectory);
        }

        public async Task VerifyChecksumAsync(
            string archivePath, ExternalToolInfo tool,
            CancellationToken cancellationToken = default)
        {
            VerifyCalls++;
            await ToolchainChecksum.VerifySha256Async(archivePath, tool.Sha256!, cancellationToken);
        }
    }

    [Fact]
    public async Task Downloader_contract_progress_and_checksum_roundtrip()
    {
        var info = ProbeToolInfo with { Sha256 = null };
        var downloader = new StubDownloader();
        var dir = Path.Combine(_root, "download");
        var reported = new List<double>();

        var reportedBy = new RecordingProgress();
        var archive = await downloader.DownloadAsync(info, dir, reportedBy);
        Assert.True(File.Exists(archive));

        // 先算出真实校验和写入元数据，再走完整校验路径
        info = info with { Sha256 = await ToolchainChecksum.ComputeSha256Async(archive) };
        await downloader.VerifyChecksumAsync(archive, info);
        Assert.Equal(1, downloader.VerifyCalls);

        var output = await downloader.ExtractFlattenedAsync(archive, Path.Combine(_root, "install"));
        Assert.True(File.Exists(Path.Combine(output, "probe-tool.exe")));

        // 进度必须单调且以 1 收尾
        Assert.Equal(new[] { 0.25, 0.75, 1.0 }, reportedBy.Values);
    }

    [Fact]
    public async Task Tampered_archive_is_rejected_by_checksum()
    {
        var downloader = new StubDownloader { Content = "payload" };
        var dir = Path.Combine(_root, "tamper");
        var archive = await downloader.DownloadAsync(ProbeToolInfo, dir);

        // 元数据携带的是**另一个内容**的校验和 → 必须拒绝进入安装
        var info = ProbeToolInfo with { Sha256 = await ToolchainChecksum.ComputeSha256Async(CreateOtherFile()) };
        await Assert.ThrowsAsync<ToolchainException>(
            () => downloader.VerifyChecksumAsync(archive, info));
    }

    private string CreateOtherFile()
    {
        var path = Path.Combine(_root, "other.bin");
        File.WriteAllText(path, "totally different payload");
        return path;
    }
}
