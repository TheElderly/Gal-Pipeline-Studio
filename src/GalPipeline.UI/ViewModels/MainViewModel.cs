using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace GalPipeline.UI.ViewModels;

/// <summary>
/// 主工作台视图模型：持有 PythonSidecarClient 生命周期，串起
/// 载入（detect + extract）→ 批量翻译（含 LQA 状态回写）→ 导出回写
/// 三段业务流；所有属性经 CommunityToolkit.Mvvm 通知界面。
/// </summary>
public sealed partial class MainViewModel : ObservableObject, IDisposable
{
    private readonly CancellationTokenSource _lifecycleCts = new();
    private PythonSidecarClient? _client;
    private ExtractToIrResult? _currentEnvelope;

    public ObservableCollection<TranslationUnitItemViewModel> Units { get; } = new();

    [ObservableProperty]
    public partial string CurrentFilePath { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string DetectedEngineName { get; set; } = string.Empty;

    [ObservableProperty]
    public partial bool IsEngineSupported { get; set; }

    [ObservableProperty]
    public partial bool IsPythonReady { get; set; }

    [ObservableProperty]
    public partial bool IsBusy { get; set; }

    [ObservableProperty]
    public partial string StatusMessage { get; set; } = "正在连接 Python 核心…";

    [ObservableProperty]
    public partial string ExportDirectory { get; set; } = "export";

    [ObservableProperty]
    public partial double ProgressPercent { get; set; }

    [ObservableProperty]
    public partial int TranslatedCount { get; set; }

    [ObservableProperty]
    public partial int FailedCount { get; set; }

    [ObservableProperty]
    public partial int TotalCount { get; set; }

    [ObservableProperty]
    public partial ApiConfigViewModel ApiConfig { get; set; } = new();

    public bool IsIdle => !IsBusy;

    private static readonly Brush ReadyBrush = new SolidColorBrush(Color.FromArgb(255, 0x46, 0xB3, 0x5B));
    private static readonly Brush FaultBrush = new SolidColorBrush(Color.FromArgb(255, 0xE0, 0x4F, 0x5F));

    /// <summary>核心就绪指示灯（规避页面级 x:Bind 函数绑定的编译器缺陷，改属性绑定）。</summary>
    public Brush StatusBrush => IsPythonReady ? ReadyBrush : FaultBrush;

    public Visibility EngineBadgeVisibility =>
        IsEngineSupported ? Visibility.Visible : Visibility.Collapsed;

    public string ProgressLabel =>
        $"已通过 {TranslatedCount} / 待翻译 {PendingCount} / 不合格 {FailedCount}，共 {TotalCount} 条";

    public int PendingCount => TotalCount - TranslatedCount - FailedCount;

    /// <summary>当前项目信封中的 Gal-IR 项目（含完整 units，供回灌导出）。</summary>
    public GalIRProjectDto? CurrentProject => _currentEnvelope?.Project;

    partial void OnIsBusyChanged(bool value) => OnPropertyChanged(nameof(IsIdle));
    partial void OnIsPythonReadyChanged(bool value) => OnPropertyChanged(nameof(StatusBrush));
    partial void OnIsEngineSupportedChanged(bool value) => OnPropertyChanged(nameof(EngineBadgeVisibility));

    partial void OnTotalCountChanged(int value)
    {
        OnPropertyChanged(nameof(PendingCount));
        OnPropertyChanged(nameof(ProgressLabel));
    }

    partial void OnTranslatedCountChanged(int value)
    {
        OnPropertyChanged(nameof(PendingCount));
        OnPropertyChanged(nameof(ProgressLabel));
    }

    partial void OnFailedCountChanged(int value)
    {
        OnPropertyChanged(nameof(PendingCount));
        OnPropertyChanged(nameof(ProgressLabel));
    }

    /// <summary>应用启动探活：拉起 Sidecar 并验证 JSON-RPC 通道。</summary>
    public async Task InitializeAsync()
    {
        try
        {
            _client = new PythonSidecarClient();
            await _client.PingAsync();
            IsPythonReady = true;
            StatusMessage = "Python 核心就绪";
        }
        catch (Exception ex)
        {
            IsPythonReady = false;
            StatusMessage = $"Python 核心连接失败：{ex.Message}";
        }
    }

    /// <summary>载入剧本：detect 认领后 extract 填充对照工作台。</summary>
    [RelayCommand]
    private async Task LoadFileAsync(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            StatusMessage = "文件不存在，请检查路径";
            return;
        }
        await RunBusyAsync(async () =>
        {
            var detect = await _client!.DetectFormatAsync(path);
            IsEngineSupported = detect.Detected;
            DetectedEngineName = detect.Detected ? EngineBadge(detect.Adapter) : "未识别";
            if (!detect.Detected)
            {
                StatusMessage = "无适配器认领该文件的引擎格式";
                return;
            }
            var envelope = await _client.ExtractToIrAsync(path);
            _currentEnvelope = envelope;
            CurrentFilePath = path;
            Units.Clear();
            foreach (var unit in envelope.Project.Units)
            {
                Units.Add(new TranslationUnitItemViewModel(unit));
            }
            RefreshProgress();
            StatusMessage = $"已加载 {envelope.UnitCount} 条可译文本";
        });
    }

    /// <summary>批量送翻：仅提取 EXTRACTED / LQA_FAILED 单元，结果按 id 回写各行。</summary>
    [RelayCommand]
    private async Task TranslateBatchAsync()
    {
        var targets = Units
            .Where(u => u.Status is "EXTRACTED" or "LQA_FAILED")
            .Select(u => u.Model)
            .ToList();
        if (targets.Count == 0)
        {
            StatusMessage = "没有待翻译单元（均已通过质检）";
            return;
        }
        await RunBusyAsync(async () =>
        {
            StatusMessage = $"正在翻译 {targets.Count} 条…";
            var updated = await _client!.TranslateBatchAsync(targets, ApiConfig.ToDto());
            var byId = updated.ToDictionary(u => u.Id);
            foreach (var item in Units)
            {
                if (byId.TryGetValue(item.Model.Id, out var dto))
                {
                    item.Update(dto);
                }
            }
            RefreshProgress();
            StatusMessage =
                $"批次完成：{updated.Count(u => u.Status == "LQA_PASSED")} 通过，"
                + $"{updated.Count(u => u.Status == "LQA_FAILED")} 待返工";
        });
    }

    /// <summary>导出回写：把当前项目（含手动微调译文）交还适配器生成汉化脚本。</summary>
    [RelayCommand]
    private async Task ExportAssetAsync(string? outputDir)
    {
        if (CurrentProject is null)
        {
            StatusMessage = "尚未加载任何项目";
            return;
        }
        var directory = string.IsNullOrWhiteSpace(outputDir) ? "export" : outputDir;
        await RunBusyAsync(async () =>
        {
            var result = await _client!.IrToAssetAsync(CurrentProject, directory);
            StatusMessage = $"导出成功：{result.OutputPath}";
        });
    }

    private async Task RunBusyAsync(Func<Task> work)
    {
        if (IsBusy)
        {
            return;
        }
        IsBusy = true;
        try
        {
            await work();
        }
        catch (JsonRpcException ex)
        {
            StatusMessage = $"Sidecar 业务错误 [{ex.Code}]：{ex.Message}";
        }
        catch (Exception ex)
        {
            StatusMessage = $"操作失败：{ex.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }

    private void RefreshProgress()
    {
        var total = Units.Count;
        var passed = Units.Count(u => u.Status == "LQA_PASSED");
        var failed = Units.Count(u => u.Status == "LQA_FAILED");
        TotalCount = total;
        TranslatedCount = passed;
        FailedCount = failed;
        ProgressPercent = total == 0 ? 0 : Math.Round((passed + failed) * 100.0 / total, 1);
    }

    private static string EngineBadge(string adapter) => adapter switch
    {
        "kag" => "KAG / KiriKiri",
        var name => name.ToUpperInvariant(),
    };

    public void Dispose()
    {
        _lifecycleCts.Cancel();
        _lifecycleCts.Dispose();
        _client?.Dispose();
    }
}
