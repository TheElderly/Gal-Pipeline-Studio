using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using GalPipeline.Core.Toolchain;

namespace GalPipeline.Core.Audio;

/// <summary>一次转码交付：可播放路径（已入缓存）或用户可读失败原因。</summary>
public sealed record TranscodeOutcome(string? PlayablePath, string? FailureReason);

/// <summary>
/// FFmpeg 转码缓存服务 —— 多媒体管线的**唯一**音频底层通道。
///
/// 背景：Galgame 语音 90% 以上是 .ogg，而 WMSDK（WPF MediaPlayer）原生
/// 解码面不含 OGG 容器。与其在提示语里让用户「自行转码」，不如把 FFmpeg
/// 确立为唯一底层：检测到无法直播的格式时，经**跨进程 CLI** 转码为
/// PCM WAV 并落入缓存目录，对播放器透明（后续视频/字幕管线复用同一依赖）。
///
/// 工具定位**不做特例**：本类不自己找 ffmpeg，直接消费注入的
/// <see cref="IToolResolver"/>（四级瀑布流 + 健康检查）；Missing 即抛
/// <see cref="ToolchainMissingException"/>（携带装配引导），与全系统
/// 其他外部工具（GARbro / FreeMote / XDelta）同一套收敛纪律。
///
/// 缓存契约：
/// * 缓存键 = SHA256(绝对路径 | 文件长度 | LastWriteTimeUtc)——源文件
///   被替换/改动即换新键，天然失效，绝不播放陈旧转码；
/// * 产物先写 ``.part`` 再原子 Move，杜绝「半截 WAV 被当作已缓存」；
/// * <see cref="GetPlayableAsync"/> 全程尊重取消令牌：取消即刻
///   ``Kill(entireProcessTree)`` 终止 FFmpeg 子进程并释放句柄 —— 快速
///   连续切行时由调用方取消上一未完成任务，不产生僵尸进程；
/// * 缓存命中**不依赖工具链**：已转码的 WAV 在 FFmpeg 缺席时仍可回放。
/// </summary>
public class FFmpegAudioCache : IDisposable
{
    private readonly IToolResolver? _toolResolver;
    private string? _resolvedFfmpegPath;

    /// <summary>默认缓存根：%TEMP%/galpipeline/cache。</summary>
    public static string DefaultCacheDirectory =>
        Path.Combine(Path.GetTempPath(), "galpipeline", "cache");

    public FFmpegAudioCache(string? cacheDirectory = null, IToolResolver? toolResolver = null)
    {
        CacheDirectory = cacheDirectory ?? DefaultCacheDirectory;
        // 未显式注入时消费动态运行时：设置页改了自定义路径 → 解析器整体
        // 替换 + ResolverChanged 失效通知，下一次转码即用新工具。
        _toolResolver = toolResolver;
        if (toolResolver is null)
        {
            ToolchainRuntime.ResolverChanged += InvalidateResolvedPath;
        }
    }

    /// <summary>订阅运行时后必须随宿主释放，防静态事件钉住实例。</summary>
    public void Dispose()
    {
        ToolchainRuntime.ResolverChanged -= InvalidateResolvedPath;
    }

    private void InvalidateResolvedPath() => _resolvedFfmpegPath = null;

    public string CacheDirectory { get; }

    /// <summary>
    /// 计算缓存产物路径。指纹含路径/长度/最后写入时间三元组：同名替换
    /// 内容即换键；同内容文件换路径也换键（成本极低，宁可重转不误播）。
    /// </summary>
    public string CachePathFor(string sourcePath)
    {
        var full = Path.GetFullPath(sourcePath);
        var info = new FileInfo(full);
        var fingerprint =
            $"{full.ToLowerInvariant()}|{info.Length}|{info.LastWriteTimeUtc.Ticks}";
        var hash = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(fingerprint))).ToLowerInvariant();
        return Path.Combine(CacheDirectory, hash + ".wav");
    }

    /// <summary>缓存是否已有可用的转码产物（比源文件新即视为有效）。</summary>
    public bool HasFreshCache(string sourcePath)
    {
        var full = Path.GetFullPath(sourcePath);
        if (!File.Exists(full))
        {
            return false;
        }
        var target = CachePathFor(full);
        if (!File.Exists(target))
        {
            return false;
        }
        return File.GetLastWriteTimeUtc(target)
               >= File.GetLastWriteTimeUtc(full);
    }

    /// <summary>
    /// 取得 sourcePath 的可播放版本：直读缓存命中，否则经 FFmpeg 转码入缓存。
    /// 取消令牌触发即终止子进程并抛 <see cref="OperationCanceledException"/>。
    /// </summary>
    public async Task<TranscodeOutcome> GetPlayableAsync(
        string sourcePath, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(sourcePath))
        {
            throw new ArgumentException("音频路径不能为空", nameof(sourcePath));
        }
        cancellationToken.ThrowIfCancellationRequested();

        var full = Path.GetFullPath(sourcePath);
        if (!File.Exists(full))
        {
            return new TranscodeOutcome(null, $"音频文件不存在：{full}");
        }

        var target = CachePathFor(full);
        if (HasFreshCache(full))
        {
            return new TranscodeOutcome(target, null);
        }

        var ffmpegPath = ResolveFfmpegPath();

        Directory.CreateDirectory(CacheDirectory);
        var temp = target + ".part";
        TryDelete(temp);

        var startInfo = new ProcessStartInfo(ffmpegPath)
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardError = true,
        };
        startInfo.ArgumentList.Add("-nostdin");
        startInfo.ArgumentList.Add("-y");
        startInfo.ArgumentList.Add("-loglevel");
        startInfo.ArgumentList.Add("error");
        startInfo.ArgumentList.Add("-i");
        startInfo.ArgumentList.Add(full);
        startInfo.ArgumentList.Add("-vn");
        startInfo.ArgumentList.Add("-acodec");
        startInfo.ArgumentList.Add("pcm_s16le");
        startInfo.ArgumentList.Add(temp);

        using var process = StartProcess(startInfo);
        if (process is null)
        {
            return new TranscodeOutcome(null, "无法启动 FFmpeg 子进程。");
        }

        // 取消 = 杀整棵进程树：这是「快速切行不留僵尸」的核心闸门
        using var killRegistration = cancellationToken.Register(() =>
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                }
            }
            catch
            {
                // 进程恰好在检查与击杀之间退出：目标已达成，忽略
            }
        });

        try
        {
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw; // 击杀已由注册回调同步完成，向上传递取消语义
        }

        if (process.ExitCode != 0)
        {
            var stderr = process.StartInfo.RedirectStandardError
                ? await process.StandardError.ReadToEndAsync().ConfigureAwait(false)
                : string.Empty;
            return new TranscodeOutcome(
                null,
                $"FFmpeg 转码失败（exit {process.ExitCode}）：{stderr.Trim()}");
        }
        if (!File.Exists(temp))
        {
            return new TranscodeOutcome(null, "FFmpeg 报告成功但未产出目标文件。");
        }

        File.Move(temp, target, overwrite: true);
        return new TranscodeOutcome(target, null);
    }

    /// <summary>
    /// 经统一工具链解析 FFmpeg 可执行路径并缓存结果（本类零特例探测）。
    /// Missing → <see cref="ToolchainMissingException"/>（装配引导直达用户）；
    /// Corrupted / InvalidVersion → 不得当 Ready 使用，抛带详情的工具链异常。
    /// </summary>
    private string ResolveFfmpegPath()
    {
        if (_resolvedFfmpegPath is not null)
        {
            return _resolvedFfmpegPath;
        }
        var info = ToolchainRegistry.Get(ToolType.FFmpeg);
        // 未注入时取运行时现值：设置页换路径后这里即刻消费新解析器
        var resolver = _toolResolver ?? ToolchainRuntime.Current;
        var status = resolver.Resolve(ToolType.FFmpeg);
        if (status.Kind is ToolStatusKind.Missing)
        {
            throw new ToolchainMissingException(info, status.Detail);
        }
        if (!status.IsReady)
        {
            throw new ToolchainException(
                $"FFmpeg 探测未通过健康检查（{status.Kind}）：{status.Detail}");
        }
        _resolvedFfmpegPath = status.Path!;
        return _resolvedFfmpegPath;
    }

    /// <summary>进程启动缝：单测以此注入假工具（不依赖 FFmpeg 在场即验证生命周期）。</summary>
    protected virtual Process? StartProcess(ProcessStartInfo startInfo) =>
        Process.Start(startInfo);

    private static void TryDelete(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // 占位文件被并发清理竞争：交给 Move 的 overwrite 兜底
        }
    }
}
