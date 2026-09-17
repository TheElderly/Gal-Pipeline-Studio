namespace GalPipeline.Core.Audio;

/// <summary>
/// 音频播放服务抽象：壳层 VM 与自动化测试消费的**唯一**播放契约。
///
/// 为什么要有这层：播放三键此前是无 Command 的死控件（可点却无声比灰掉
/// 更糟）。VM 面向本接口编程，WPF 宿主以 <c>System.Windows.Media.MediaPlayer</c>
/// 实现之 —— 解码与回放由媒体管道在后台完成，天然不阻塞 UI 线程；
/// 单测工程（net10.0 无 WPF）则可用可编程假件验证 VM 的分流逻辑。
///
/// 实现方约定：
/// * <see cref="Play"/> 必须非阻塞（立即返回，解码异步进行）；
/// * 解码失败 / 格式不支持 / 文件中途消失，经 <see cref="PlaybackFailed"/>
///   上报**用户可读**的原因，绝不允许异常冒泡打穿调用方；
/// * <see cref="Pause"/> 后 <see cref="Resume"/> 必须从暂停点继续。
/// </summary>
public interface IAudioPlayer : IDisposable
{
    /// <summary>当前是否持有活跃会话（播放中或已暂停）。</summary>
    bool HasActiveSession { get; }

    /// <summary>是否处于暂停态（<see cref="Pause"/> 后、<see cref="Resume"/>/重播前）。</summary>
    bool IsPaused { get; }

    /// <summary>自然播放结束（非人为 Stop）。</summary>
    event EventHandler? PlaybackEnded;

    /// <summary>播放失败；载荷为用户可读原因（含格式不支持等降级提示）。</summary>
    event EventHandler<string>? PlaybackFailed;

    /// <summary>播放指定音频文件（绝对路径）；重复调用视为切曲（旧会话作废）。</summary>
    void Play(string filePath);

    /// <summary>暂停当前会话（保留进度，<see cref="Resume"/> 续播）。</summary>
    void Pause();

    /// <summary>从暂停点继续。</summary>
    void Resume();

    /// <summary>终止会话并丢弃进度。</summary>
    void Stop();
}
