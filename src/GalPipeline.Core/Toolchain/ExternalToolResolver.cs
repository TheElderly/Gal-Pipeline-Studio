using System.Diagnostics;
using System.Text.RegularExpressions;

namespace GalPipeline.Core.Toolchain;

/// <summary>
/// 四级探测瀑布流的统一实现。严格优先级：
///
/// 1. **显式自定义**：调用方注入的自定义绝对路径（设置页 / 注册表映射）；
/// 2. **本地工具箱**：软件内聚目录 {toolboxRoot}/{tools/{tool}/{exe}}；
/// 3. **环境变量 PATH**：逐目录检索匹配可执行名；
/// 4. **Missing**：合法结论，返回状态绝不抛异常。
///
/// 命中即触发轻量健康检查（ProbeVersion）：跑一次 ExpectedVersionArgs
/// 命令，用 VersionPattern 提取版本号 —— 退出码异常判 Corrupted，
/// 输出不匹配判 InvalidVersion，超时判 Corrupted。**Ready 的语义是
/// 「该路径下确实是一个能跑、且自称版本可读的目标工具」**，消费方
/// 拿到即可直接起子进程，无需二次验证。
///
/// 本类无状态（每次 Resolve 现查现探），缓存策略归消费方 —— 避免
/// 「缓存了失效路径」与「缓存了 Corrupted 的暂时性故障」两类坑。
/// </summary>
public sealed class ExternalToolResolver : IToolResolver
{
    private const int ProbeTimeoutMs = 10_000;

    private readonly IReadOnlyDictionary<ToolType, ExternalToolInfo> _catalog;
    private readonly IReadOnlyDictionary<ToolType, string>? _customPaths;
    private readonly string _toolboxRoot;
    private readonly IReadOnlyList<string> _searchPaths;

    public ExternalToolResolver(
        IReadOnlyDictionary<ToolType, ExternalToolInfo>? catalog = null,
        IReadOnlyDictionary<ToolType, string>? customPaths = null,
        string? toolboxRoot = null,
        IReadOnlyList<string>? searchPaths = null)
    {
        _catalog = catalog ?? ToolchainRegistry.Catalog();
        _customPaths = customPaths;
        _toolboxRoot = toolboxRoot ?? AppContext.BaseDirectory;
        _searchPaths = searchPaths ?? DefaultPathDirectories();
    }

    /// <summary>系统 PATH 的目录切分（供默认构造；测试可注入替代）。</summary>
    public static IReadOnlyList<string> DefaultPathDirectories() =>
        (Environment.GetEnvironmentVariable("PATH") ?? string.Empty)
        .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    public ToolStatus Resolve(ToolType type)
    {
        if (!_catalog.TryGetValue(type, out var info))
        {
            return new ToolStatus(
                ToolStatusKind.Missing, type,
                Detail: $"工具链注册表中没有 {type} 的元数据（无法探测）");
        }

        // 优先级 1：显式自定义路径（配置了但失效时**继续瀑布**——旧配置
        // 不该挡住本地工具箱里现成的可用副本）
        if (_customPaths is not null
            && _customPaths.TryGetValue(type, out var custom)
            && !string.IsNullOrWhiteSpace(custom))
        {
            if (File.Exists(custom))
            {
                return Probe(info, custom, ToolOrigin.CustomOverride);
            }
        }

        // 优先级 2：本地工具箱（软件内聚目录）
        var toolboxCandidate = Path.Combine(_toolboxRoot, info.ToolboxRelativePath);
        if (File.Exists(toolboxCandidate))
        {
            return Probe(info, toolboxCandidate, ToolOrigin.LocalToolbox);
        }

        // 优先级 3：PATH
        foreach (var directory in _searchPaths)
        {
            var candidate = Path.Combine(directory, info.ExecutableName);
            if (File.Exists(candidate))
            {
                return Probe(info, candidate, ToolOrigin.PathEnvironment);
            }
        }

        // 优先级 4：Missing（合法结论，携带装配引导所需的全部元数据）
        return new ToolStatus(
            ToolStatusKind.Missing,
            type,
            Detail: $"四级瀑布流未命中：自定义路径 / 工具箱 {info.ToolboxRelativePath} / PATH 均无 {info.ExecutableName}");
    }

    private ToolStatus Probe(ExternalToolInfo info, string path, ToolOrigin origin)
    {
        Process? process;
        try
        {
            var startInfo = new ProcessStartInfo(path)
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var argument in info.ExpectedVersionArgs)
            {
                startInfo.ArgumentList.Add(argument);
            }
            process = Process.Start(startInfo);
        }
        catch (Exception ex)
        {
            // 文件在 Exists 与 Start 之间消失 / 权限不足等：按损坏上报，不吞
            return Corrupted(info.Type, path, origin, $"探测进程无法启动：{ex.Message}");
        }
        if (process is null)
        {
            return Corrupted(info.Type, path, origin, "探测进程启动返回空句柄");
        }

        using var _ = process;
        var stdoutTask = process.StandardOutput.ReadToEndAsync();
        var stderrTask = process.StandardError.ReadToEndAsync();
        try
        {
            if (!process.WaitForExit(ProbeTimeoutMs))
            {
                process.Kill(entireProcessTree: true);
                return Corrupted(info.Type, path, origin, $"版本探测超时（>{ProbeTimeoutMs / 1000.0:0}s）已终止");
            }
        }
        catch
        {
            return Corrupted(info.Type, path, origin, "版本探测等待失败（进程可能已损坏）");
        }

        var output = (stdoutTask.GetAwaiter().GetResult()
                      + "\n" + stderrTask.GetAwaiter().GetResult()).Trim();
        if (process.ExitCode != 0)
        {
            return Corrupted(
                info.Type, path, origin,
                $"探测命令退出码 {process.ExitCode}：{Tail(output)}");
        }

        string version;
        try
        {
            var regex = new Regex(
                info.VersionPattern,
                RegexOptions.IgnoreCase | RegexOptions.CultureInvariant,
                TimeSpan.FromMilliseconds(500));
            var match = regex.Match(output);
            if (!match.Success)
            {
                return new ToolStatus(
                    ToolStatusKind.InvalidVersion, info.Type,
                    Path: path, Origin: origin,
                    Detail: $"可执行但版本号未匹配预期模式（输出片段：{Tail(output)}）");
            }
            version = match.Groups.Count > 1 ? match.Groups[1].Value : match.Value;
        }
        catch (Exception ex) when (ex is RegexParseException or RegexMatchTimeoutException)
        {
            return new ToolStatus(
                ToolStatusKind.InvalidVersion, info.Type,
                Path: path, Origin: origin,
                Detail: $"VersionPattern 非法或匹配超时：{ex.Message}");
        }

        return new ToolStatus(
            ToolStatusKind.Ready, info.Type,
            Path: path, Version: version, Origin: origin);
    }

    private static ToolStatus Corrupted(ToolType type, string path, ToolOrigin origin, string detail) =>
        new(ToolStatusKind.Corrupted, type, Path: path, Detail: detail, Origin: origin);

    private static string Tail(string text)
    {
        text = text.ReplaceLineEndings(" ⏎ ");
        return text.Length <= 160 ? text : text[^160..];
    }
}
