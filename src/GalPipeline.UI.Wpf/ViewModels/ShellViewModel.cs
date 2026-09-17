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
    public partial string SidecarStatusText { get; set; } = "Sidecar Engine: Connecting…";

    [ObservableProperty]
    public partial Brush SidecarStatusBrush { get; set; } = PendingBrush;

    [ObservableProperty]
    public partial string StudioFileLabel { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string StudioMetricsLabel { get; set; } = string.Empty;

    /// <summary>内容区说明条的实时文案：空闲时回落为设计稿原文，有动作时显示 Studio 侧状态。</summary>
    [ObservableProperty]
    public partial string StudioStatusLabel { get; set; } = IdleHint;

    /// <summary>设计稿规定的内容区说明条原文（空闲态，保证静置时版面与设计稿一致）。</summary>
    public const string IdleHint = "Virtualized - DataGrid : compact ergonomics, macro protection";

    /// <summary>接收 Studio 侧通报（文件行数 / Token 与延迟 / 状态文案），汇入主窗底栏与说明条。</summary>
    public void PublishStudioMetrics(string fileLabel, string metrics, string status)
    {
        StudioFileLabel = fileLabel;
        StudioMetricsLabel = metrics;
        StudioStatusLabel = string.IsNullOrWhiteSpace(status) ? IdleHint : status;
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
            SidecarStatusText = "Sidecar Engine: Ready";
        }
        catch (Exception ex)
        {
            SidecarStatusBrush = FaultBrush;
            SidecarStatusText = $"Sidecar Engine: Fault — {ex.Message}";
        }
    }

    public void Dispose()
    {
        _client?.Dispose();
    }
}
