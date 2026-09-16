using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// 进程级共享 Sidecar 客户端：Settings 页随 Frame 导航反复重建视图，
/// 静态惰性单例保证不会因导航而反复拉起 Python 子进程。
/// </summary>
public static class SidecarClientProvider
{
    private static readonly Lazy<PythonSidecarClient> ClientLazy =
        new(() => new PythonSidecarClient(), LazyThreadSafetyMode.ExecutionAndPublication);

    public static PythonSidecarClient Client => ClientLazy.Value;
}

/// <summary>
/// Settings 页视图模型：OpenAI 兼容端点 / 凭据 / reasoning_effort 推理力度 /
/// 采样温度配置，以及经 Python Sidecar fetch_models 的模型清单拉取。
/// </summary>
public sealed partial class SettingsViewModel : ObservableObject
{
    public IReadOnlyList<string> ReasoningEffortOptions { get; } =
        ["low", "medium", "high", "max"];

    public ObservableCollection<string> Models { get; } = [];

    [ObservableProperty]
    public partial string ApiBaseUrl { get; set; } = "https://open.bigmodel.cn/api/paas/v4";

    [ObservableProperty]
    public partial string ApiKey { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string? SelectedReasoningEffort { get; set; } = "medium";

    [ObservableProperty]
    public partial double Temperature { get; set; } = 0.3;

    /// <summary>温度卡片副标题（SettingsCard.Description 绑定源）。</summary>
    public string TemperatureDescription => $"采样温度：{Temperature:0.00}";

    partial void OnTemperatureChanged(double value) =>
        OnPropertyChanged(nameof(TemperatureDescription));

    [ObservableProperty]
    public partial string? SelectedModel { get; set; }

    [ObservableProperty]
    public partial bool IsRefreshingModels { get; set; }

    [ObservableProperty]
    public partial string ModelRefreshStatus { get; set; } = "尚未拉取模型清单。";

    /// <summary>刷新模型列表：经 Python Sidecar 的 fetch_models 跨进程转发。</summary>
    /// <remarks>
    /// AsyncRelayCommand 默认禁止并发执行（执行期间按钮自动禁用）；
    /// 三类失败（网络/401/非 JSON）已被 Python 侧归一为 -32000 业务错误，
    /// 经 JsonRpcException 还原为界面可读状态，不抛出。
    /// </remarks>
    [RelayCommand]
    private async Task RefreshModelsAsync(CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(ApiBaseUrl))
        {
            ModelRefreshStatus = "API Base URL 不能为空。";
            return;
        }
        IsRefreshingModels = true;
        ModelRefreshStatus = "正在拉取模型清单…";
        try
        {
            var models = await SidecarClientProvider.Client.FetchModelsAsync(
                ApiBaseUrl,
                string.IsNullOrWhiteSpace(ApiKey) ? null : ApiKey,
                cancellationToken);

            Models.Clear();
            foreach (var model in models)
            {
                Models.Add(model);
            }
            SelectedModel = models.Count > 0 ? models[0] : null;
            ModelRefreshStatus = $"已拉取 {models.Count} 个模型。";
        }
        catch (JsonRpcException ex)
        {
            ModelRefreshStatus = $"拉取失败 [{ex.Code}]：{ex.Message}";
        }
        catch (OperationCanceledException)
        {
            ModelRefreshStatus = "拉取已取消。";
        }
        catch (Exception ex)
        {
            ModelRefreshStatus = $"拉取失败：{ex.Message}";
        }
        finally
        {
            IsRefreshingModels = false;
        }
    }

    /// <summary>以当前设置组装翻译调度配置（供阶段三后续的翻译命令直接下发）。</summary>
    public TranslationConfigDto ToConfig() => new(
        ApiBase: ApiBaseUrl,
        ModelName: SelectedModel ?? "glm-4-flash",
        ApiKey: string.IsNullOrWhiteSpace(ApiKey) ? null : ApiKey,
        Temperature: Temperature,
        ReasoningEffort: SelectedReasoningEffort);
}
