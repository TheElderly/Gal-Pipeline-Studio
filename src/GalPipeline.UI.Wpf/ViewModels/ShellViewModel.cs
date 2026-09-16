using CommunityToolkit.Mvvm.ComponentModel;
using GalPipeline.Core.IPC;
using System.Windows.Media;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// 主壳视图模型：Sidecar 探活状态、全局 Token 消耗占位与当前页标题，
/// 供底部全局状态栏消费。
/// </summary>
public sealed partial class ShellViewModel : ObservableObject, IDisposable
{
    private static readonly Brush ReadyBrush = new SolidColorBrush(Color.FromRgb(0x46, 0xB3, 0x5B));
    private static readonly Brush FaultBrush = new SolidColorBrush(Color.FromRgb(0xE0, 0x4F, 0x5F));
    private static readonly Brush PendingBrush = new SolidColorBrush(Color.FromRgb(0x8A, 0x8A, 0x8A));

    private PythonSidecarClient? _client;

    [ObservableProperty]
    public partial string SidecarStatusText { get; set; } = "正在连接 Python 核心…";

    [ObservableProperty]
    public partial Brush SidecarStatusBrush { get; set; } = PendingBrush;

    [ObservableProperty]
    public partial string TokenUsageText { get; set; } = "Token 消耗：0（本次会话）";

    [ObservableProperty]
    public partial string StudioFileLabel { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string StudioMetricsLabel { get; set; } = string.Empty;

    /// <summary>接收 Studio 侧通报（文件行数 / Token / Latency），汇入主窗底栏。</summary>
    public void PublishStudioMetrics(string fileLabel, string metrics)
    {
        StudioFileLabel = fileLabel;
        StudioMetricsLabel = metrics;
    }

    [ObservableProperty]
    public partial string CurrentPageTitle { get; set; } = "Studio 剧本工坊";

    /// <summary>应用启动探活：拉起 Sidecar 并验证 JSON-RPC 通道，点亮状态栏。</summary>
    public async Task InitializeAsync()
    {
        try
        {
            _client = new PythonSidecarClient();
            await _client.PingAsync();
            SidecarStatusBrush = ReadyBrush;
            SidecarStatusText = "Python 核心就绪";
        }
        catch (Exception ex)
        {
            SidecarStatusBrush = FaultBrush;
            SidecarStatusText = $"Python 核心连接失败：{ex.Message}";
        }
    }

    /// <summary>Token 用量由翻译批次回传后累计（阶段三后续切片接通，现为占位）。</summary>
    public void ReportTokenUsage(int promptTokens, int completionTokens)
    {
        TokenUsageText = $"Token 消耗：{promptTokens + completionTokens}（本次会话）";
    }

    public void Dispose()
    {
        _client?.Dispose();
    }
}
