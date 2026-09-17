using System.Windows.Media;
using GalPipeline.Core.Audio;

namespace GalPipeline.Desktop.Services;

/// <summary>
/// <see cref="IAudioPlayer"/> 的 WPF 宿主实现：基于
/// <c>System.Windows.Media.MediaPlayer</c>（WMSDK 管道）。
///
/// 解码与回放由媒体管道异步完成，<see cref="Play"/> 立即返回 —— 不阻塞 UI
/// 线程。失败面全部收敛为 <see cref="PlaybackFailed"/> 事件（用户可读文案），
/// 绝不让异常冒泡；WPF 内置解码器不支持 OGG 等容器，失败文案里直接给出
/// 「转码为 WAV/MP3」的出路，而不是让用户对着无声按钮猜。
///
/// 实例必须由 UI 线程创建（MediaPlayer 是 DispatcherObject），事件回调
/// 因此也在 UI 线程触发，VM 侧无需额外封送。
/// </summary>
public sealed class WpfAudioPlayer : IAudioPlayer
{
    private readonly MediaPlayer _media = new();

    public bool HasActiveSession { get; private set; }

    public bool IsPaused { get; private set; }

    public event EventHandler? PlaybackEnded;

    public event EventHandler<string>? PlaybackFailed;

    public WpfAudioPlayer()
    {
        _media.MediaEnded += (_, _) =>
        {
            HasActiveSession = false;
            IsPaused = false;
            PlaybackEnded?.Invoke(this, EventArgs.Empty);
        };
        _media.MediaFailed += (_, e) =>
        {
            HasActiveSession = false;
            IsPaused = false;
            var reason = e.ErrorException?.Message ?? "未知解码错误";
            // 正常路径下 OGG 等非 WMSDK 容器已由 FFmpegAudioCache 先行归一为
            // WAV，此事件只剩「文件损坏 / 容器伪标」等真实故障可触达。
            PlaybackFailed?.Invoke(this, $"音频解码失败：{reason}");
        };
    }

    public void Play(string filePath)
    {
        if (string.IsNullOrWhiteSpace(filePath))
        {
            throw new ArgumentException("音频路径不能为空", nameof(filePath));
        }
        Stop();
        _media.Open(new Uri(filePath, UriKind.Absolute));
        _media.Play();
        HasActiveSession = true;
        IsPaused = false;
    }

    public void Pause()
    {
        if (!HasActiveSession || IsPaused)
        {
            return;
        }
        _media.Pause();
        IsPaused = true;
    }

    public void Resume()
    {
        if (!HasActiveSession || !IsPaused)
        {
            return;
        }
        _media.Play();
        IsPaused = false;
    }

    public void Stop()
    {
        _media.Stop();
        _media.Close();
        HasActiveSession = false;
        IsPaused = false;
    }

    public void Dispose() => Stop();
}
