using System.Collections.ObjectModel;
using System.Diagnostics;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;
using GalPipeline.Core.Toolchain;
using GalPipeline.Core.Translation;
using Microsoft.Win32;

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
///
/// **本页是翻译调度的唯一真源**：任一配置变更即整体快照写入
/// <see cref="TranslationSettingsStore"/>，Studio 的批量翻译与单条重试在
/// 命令发起时读取快照 —— 「设置页调了、翻译仍走写死默认值」的软花架子
/// 由此根除（此前两处各自持有一份 ApiBase/ModelName，永不互相通知）。
/// </summary>
public sealed partial class SettingsViewModel : ObservableObject
{
    /// <summary>推理力度选项；「off」表示不向载荷注入 reasoning_effort
    /// （非推理模型收到该字段会被部分端点以 400 拒收，故必须可选）。</summary>
    public IReadOnlyList<string> ReasoningEffortOptions { get; } =
        ["off", "low", "medium", "high", "max"];

    public ObservableCollection<string> Models { get; } = [];

    [ObservableProperty]
    public partial string ApiBaseUrl { get; set; } = TranslationSettingsStore.Current.ApiBase;

    [ObservableProperty]
    public partial string ApiKey { get; set; } = TranslationSettingsStore.Current.ApiKey ?? string.Empty;

    [ObservableProperty]
    public partial string? SelectedReasoningEffort { get; set; } =
        EffortToOption(TranslationSettingsStore.Current.ReasoningEffort);

    [ObservableProperty]
    public partial double Temperature { get; set; } = TranslationSettingsStore.Current.Temperature;

    [ObservableProperty]
    public partial string? SelectedModel { get; set; } = TranslationSettingsStore.Current.ModelName;

    /// <summary>温度卡片副标题（SettingsCard.Description 绑定源）。</summary>
    public string TemperatureDescription => $"采样温度：{Temperature:0.00}";

    private static string? OptionToEffort(string? option) =>
        string.IsNullOrWhiteSpace(option) || option == "off" ? null : option;

    private static string EffortToOption(string? effort) => effort ?? "off";

    /// <summary>任一配置变更 → 整体快照写入进程级仓库（原子引用切换）。
    /// **必须携带当前工具链映射**：快照是整体替换语义，不带映射 = 静默清空
    /// 用户配置的自定义工具路径（本页同时是两者的编辑入口，最容易踩）。</summary>
    private void PushToStore() =>
        TranslationSettingsStore.Update(new TranslationSettingsSnapshot(
            ApiBase: string.IsNullOrWhiteSpace(ApiBaseUrl) ? TranslationSettingsStore.DefaultMockApiBase : ApiBaseUrl.Trim(),
            ApiKey: TranslationSettingsStore.NormalizeKey(ApiKey),
            ModelName: string.IsNullOrWhiteSpace(SelectedModel)
                ? TranslationSettingsStore.DefaultModelName
                : SelectedModel.Trim(),
            Temperature: Temperature,
            ReasoningEffort: OptionToEffort(SelectedReasoningEffort),
            CustomToolPaths: TranslationSettingsStore.Current.CustomToolPaths));

    partial void OnApiBaseUrlChanged(string value) => PushToStore();

    partial void OnApiKeyChanged(string value) => PushToStore();

    partial void OnSelectedReasoningEffortChanged(string? value) => PushToStore();

    partial void OnSelectedModelChanged(string? value) => PushToStore();

    partial void OnTemperatureChanged(double value)
    {
        OnPropertyChanged(nameof(TemperatureDescription));
        PushToStore();
    }

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

    /// <summary>以当前设置组装翻译调度配置（与 PushToStore 同一口径）。</summary>
    public TranslationConfigDto ToConfig()
    {
        var snapshot = TranslationSettingsStore.Current;
        return new TranslationConfigDto(
            ApiBase: snapshot.ApiBase,
            ModelName: snapshot.ModelName,
            ApiKey: snapshot.ApiKey,
            Temperature: snapshot.Temperature,
            ReasoningEffort: snapshot.ReasoningEffort);
    }

    // -------------------------------------------------------------------
    // 外部工具链环境（Toolchain Environment）
    // -------------------------------------------------------------------

    /// <summary>工具链状态行集合：按 DefaultTools 注册序渲染（FFmpeg 恒在首位）。</summary>
    public ObservableCollection<ToolchainToolViewModel> ToolItems { get; } = [];

    public SettingsViewModel()
    {
        foreach (var info in ToolchainRegistry.DefaultTools)
        {
            ToolItems.Add(new ToolchainToolViewModel(info, RefreshToolStatuses));
        }
        RefreshToolStatuses();
    }

    /// <summary>重新探测全部工具（一级瀑布流全量重跑 + 行 UI 刷新）。</summary>
    [RelayCommand]
    private void ReprobeAll() => RefreshToolStatuses();

    /// <summary>重建全部行的探测结论（含徽章 / 版本 / 来源 / 路径 / 自定义态）。</summary>
    private void RefreshToolStatuses()
    {
        foreach (var row in ToolItems)
        {
            row.Refresh();
        }
        var ready = ToolItems.Count(t => t.IsReady);
        ToolchainSummary = $"外部工具 {ready} / {ToolItems.Count} 就绪";
        OnPropertyChanged(nameof(ToolchainSummary));
    }

    [ObservableProperty]
    public partial string ToolchainSummary { get; set; } = string.Empty;
}

/// <summary>
/// 工具链设置页的**单行状态视图**：探测结论、来源标签、生效路径与
/// 三个动作命令（浏览配置 / 恢复默认 / 打开目录）。
/// 命令通过回调驱动父 VM 全量刷新 —— 行自身只报告事实，聚合语义不重复实现。
/// </summary>
public sealed partial class ToolchainToolViewModel : ObservableObject
{
    private readonly Action _invalidateParent;

    public ToolchainToolViewModel(ExternalToolInfo info, Action invalidateParent)
    {
        Info = info;
        _invalidateParent = invalidateParent;
        BrowseCommand = new RelayCommand(Browse);
        ResetCommand = new RelayCommand(Reset);
        OpenDirectoryCommand = new RelayCommand(OpenDirectory);
    }

    private ExternalToolInfo Info { get; }

    public ToolType ToolType => Info.Type;

    public string DisplayName => Info.DisplayName;

    public string PurposeText => Info.IsMandatory
        ? $"{Info.Purpose}（核心必选依赖）"
        : Info.Purpose;

    public string ExecutableName => Info.ExecutableName;

    /// <summary>探测结论徽章文案（Ready / Missing / Corrupted / InvalidVersion）。</summary>
    [ObservableProperty]
    public partial string StatusKindText { get; set; } = "Missing";

    /// <summary>徽章配色：Ready 绿 / Missing 灰 / 其余红（结论语义直接映射观感）。</summary>
    [ObservableProperty]
    public partial System.Windows.Media.Brush StatusBrush { get; set; } =
        System.Windows.Media.Brushes.Gray;

    /// <summary>Ready 时展示实测版本号；其余态为 em dash。</summary>
    [ObservableProperty]
    public partial string VersionText { get; set; } = "—";

    /// <summary>命中来源标签：Custom / Local / PATH（四级瀑布流的层级回执）。</summary>
    [ObservableProperty]
    public partial string OriginLabel { get; set; } = "—";

    /// <summary>当前解析生效的绝对路径；未就绪时展示引导性 Detail。</summary>
    [ObservableProperty]
    public partial string ResolvedPath { get; set; } = string.Empty;

    /// <summary>是否配置了自定义路径（「恢复默认」按钮的启用依据）。</summary>
    [ObservableProperty]
    public partial bool HasCustomPath { get; set; }

    [ObservableProperty]
    public partial bool IsReady { get; set; }

    public System.Windows.Input.ICommand BrowseCommand { get; }

    public System.Windows.Input.ICommand ResetCommand { get; }

    public System.Windows.Input.ICommand OpenDirectoryCommand { get; }

    /// <summary>重跑探测并刷新本行（工具链映射或设置变更后调用）。</summary>
    public void Refresh()
    {
        var status = ToolchainRuntime.Current.Resolve(Info.Type);
        IsReady = status.IsReady;
        StatusKindText = status.Kind switch
        {
            ToolStatusKind.Ready => "Ready",
            ToolStatusKind.Missing => "Missing",
            ToolStatusKind.Corrupted => "Corrupted",
            _ => "InvalidVersion",
        };
        StatusBrush = status.Kind switch
        {
            ToolStatusKind.Ready => FrozenBrush("#4ADE80"),
            ToolStatusKind.Missing => FrozenBrush("#9CA3AF"),
            _ => FrozenBrush("#F87171"),
        };
        VersionText = status.IsReady ? $"v{status.Version}" : "—";
        OriginLabel = status.Origin switch
        {
            ToolOrigin.CustomOverride => "Custom",
            ToolOrigin.LocalToolbox => "Local",
            ToolOrigin.PathEnvironment => "PATH",
            _ => "—",
        };
        ResolvedPath = status.IsReady
            ? status.Path!
            : status.Detail ?? "未找到（可浏览选择可执行文件，或参照 DownloadUrl 装配）";
        HasCustomPath = TranslationSettingsStore.GetCustomToolPath(Info.Type) is not null;
    }

    private void Browse()
    {
        var dialog = new OpenFileDialog
        {
            Title = $"定位 {Info.DisplayName}",
            // 限制匹配 ExecutableName：选错文件会在探测期被判 Corrupted/InvalidVersion
            Filter = $"{Info.DisplayName} ({Info.ExecutableName})|{Info.ExecutableName}|可执行文件 (*.exe)|*.exe|所有文件 (*.*)|*.*",
            CheckFileExists = true,
        };
        if (dialog.ShowDialog() != true)
        {
            return;
        }
        TranslationSettingsStore.SetCustomToolPath(Info.Type, dialog.FileName);
        Refresh();
        _invalidateParent();
    }

    private void Reset()
    {
        // 清除 = 删除该映射条目：回退 本地 tools/ → PATH 的自动瀑布探测
        TranslationSettingsStore.SetCustomToolPath(Info.Type, null);
        Refresh();
        _invalidateParent();
    }

    private void OpenDirectory()
    {
        if (!IsReady || string.IsNullOrWhiteSpace(ResolvedPath))
        {
            return;
        }
        // /select 定位文件本身（比打开目录更精确：工具可能深埋子目录）
        Process.Start(new ProcessStartInfo("explorer.exe")
        {
            ArgumentList = { "/select,", ResolvedPath },
            UseShellExecute = true,
        });
    }

    private static System.Windows.Media.Brush FrozenBrush(string hex)
    {
        var brush = new System.Windows.Media.SolidColorBrush(
            (System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(hex));
        brush.Freeze();
        return brush;
    }
}
