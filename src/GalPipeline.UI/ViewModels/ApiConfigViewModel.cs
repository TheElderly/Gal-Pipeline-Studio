using CommunityToolkit.Mvvm.ComponentModel;
using GalPipeline.Core.IPC;

namespace GalPipeline.UI.ViewModels;

/// <summary>OpenAI 兼容端点配置的界面投影；ToDto 供翻译批次直接下发。</summary>
public sealed partial class ApiConfigViewModel : ObservableObject
{
    [ObservableProperty]
    public partial string ApiBaseUrl { get; set; } = "https://open.bigmodel.cn/api/paas/v4";

    [ObservableProperty]
    public partial string ApiKey { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string ModelName { get; set; } = "glm-4-flash";

    [ObservableProperty]
    public partial int BatchSize { get; set; } = 8;

    public TranslationConfigDto ToDto() => new(
        ApiBase: ApiBaseUrl,
        ModelName: ModelName,
        ApiKey: string.IsNullOrWhiteSpace(ApiKey) ? null : ApiKey,
        BatchSize: BatchSize);
}
