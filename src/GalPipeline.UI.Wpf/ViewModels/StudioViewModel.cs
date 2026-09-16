using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Windows.Data;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// Studio 剧本工坊视图模型：载入 → 双栏审校（筛选/搜索/批量翻译/LQA 门禁/
/// 人工微调/单条重试）→ 导出回写；右栏情境审查抽屉（立绘/波形/时长/词典）。
/// 批量翻译默认指向本地环回 Mock（127.0.0.1:18080/v1），零额度离线演练。
/// </summary>
public sealed partial class StudioViewModel : ObservableObject, IDisposable
{
    /// <summary>本地环回 Mock 端点：tests/fixtures/mock_llm_server.py。</summary>
    public const string DefaultMockApiBase = "http://127.0.0.1:18080/v1";

    private GalIRProjectDto? _currentProject;

    public ObservableCollection<StudioUnitItemViewModel> Units { get; } = [];

    public ICollectionView UnitsView { get; }

    [ObservableProperty]
    public partial string CurrentFilePath { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string TranslateApiBase { get; set; } = DefaultMockApiBase;

    [ObservableProperty]
    public partial string ModelName { get; set; } = "glm-4-flash";

    [ObservableProperty]
    public partial bool IsBusy { get; set; }

    [ObservableProperty]
    public partial string StatusMessage { get; set; } = "载入 .ks 剧本以开始审校。";

    [ObservableProperty]
    public partial double ProgressPercent { get; set; }

    [ObservableProperty]
    public partial int PassedCount { get; set; }

    [ObservableProperty]
    public partial int FailedCount { get; set; }

    [ObservableProperty]
    public partial int TotalCount { get; set; }

    [ObservableProperty]
    public partial string SearchText { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string FilterMode { get; set; } = "All";

    [ObservableProperty]
    public partial StudioUnitItemViewModel? SelectedUnit { get; set; }

    [ObservableProperty]
    public partial bool IsInspectorOpen { get; set; } = true;

    [ObservableProperty]
    public partial string ExportDirectory { get; set; } = "tests/fixtures/output";

    [ObservableProperty]
    public partial string LatencyText { get; set; } = "Latency: —";

    [ObservableProperty]
    public partial string TokenUsageText { get; set; } = "Session Tokens: —";

    public ObservableCollection<int> WaveformBars { get; } = [];

    public ObservableCollection<string> GlossaryItems { get; } = [];

    public bool HasSelectedUnit => SelectedUnit is not null;

    public System.Windows.Visibility InspectorVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorEmptyVisibility =>
        IsInspectorOpen && SelectedUnit is null
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorSelectedVisibility =>
        IsInspectorOpen && SelectedUnit is not null
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public bool IsIdle => !IsBusy;

    public int PendingCount => TotalCount - PassedCount - FailedCount;

    public string BatchTranslateLabel => $"Batch Translate ({PendingCount})";

    public string ProgressLabel =>
        $"已通过 {PassedCount} / 待翻译 {PendingCount} / 不合格 {FailedCount}，共 {TotalCount} 条";

    public string FileStatusLabel =>
        string.IsNullOrWhiteSpace(CurrentFilePath)
            ? "未载入文件"
            : $"{Path.GetFileName(CurrentFilePath)} · {TotalCount} 行";

    partial void OnIsBusyChanged(bool value) => OnPropertyChanged(nameof(IsIdle));
    partial void OnTotalCountChanged(int value)
    {
        OnPropertyChanged(nameof(PendingCount));
        OnPropertyChanged(nameof(BatchTranslateLabel));
        OnPropertyChanged(nameof(ProgressLabel));
        OnPropertyChanged(nameof(FileStatusLabel));
    }
    partial void OnPassedCountChanged(int value)
    {
        OnPropertyChanged(nameof(ProgressLabel));
        OnPropertyChanged(nameof(BatchTranslateLabel));
    }
    partial void OnFailedCountChanged(int value)
    {
        OnPropertyChanged(nameof(ProgressLabel));
        OnPropertyChanged(nameof(BatchTranslateLabel));
    }
    partial void OnSearchTextChanged(string value) => UnitsView.Refresh();
    partial void OnFilterModeChanged(string value) => UnitsView.Refresh();
    partial void OnCurrentFilePathChanged(string value) => OnPropertyChanged(nameof(FileStatusLabel));
    partial void OnIsInspectorOpenChanged(bool value)
    {
        OnPropertyChanged(nameof(InspectorVisibility));
        OnPropertyChanged(nameof(InspectorEmptyVisibility));
        OnPropertyChanged(nameof(InspectorSelectedVisibility));
    }
    partial void OnSelectedUnitChanged(StudioUnitItemViewModel? value)
    {
        OnPropertyChanged(nameof(HasSelectedUnit));
        OnPropertyChanged(nameof(InspectorEmptyVisibility));
        OnPropertyChanged(nameof(InspectorSelectedVisibility));
        OnPropertyChanged(nameof(DurationLabel));
        OnPropertyChanged(nameof(CompLabel));
        RebuildInspector(value);
    }

    /// <summary>抽屉折叠开关。</summary>
    [RelayCommand]
    private void ToggleInspector() => IsInspectorOpen = !IsInspectorOpen;

    public StudioViewModel()
    {
        UnitsView = CollectionViewSource.GetDefaultView(Units);
        UnitsView.Filter = FilterUnit;
    }

    private bool FilterUnit(object item)
    {
        if (item is not StudioUnitItemViewModel unit)
        {
            return false;
        }
        var pass = FilterMode switch
        {
            "Pending" => unit.IsPending,
            "Failed" => unit.IsFailed,
            _ => true,
        };
        if (!pass)
        {
            return false;
        }
        if (string.IsNullOrWhiteSpace(SearchText))
        {
            return true;
        }
        return unit.SourceText.Contains(SearchText, StringComparison.OrdinalIgnoreCase)
               || (unit.TranslatedText?.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ?? false)
               || unit.Speaker.Contains(SearchText, StringComparison.OrdinalIgnoreCase)
               || unit.Id.Contains(SearchText, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>筛选器胶囊：All / Pending / Failed。</summary>
    [RelayCommand]
    private void SetFilter(string? mode) => FilterMode = string.IsNullOrWhiteSpace(mode) ? "All" : mode;

    /// <summary>载入剧本：detect 认领后 extract 填充审校表格。</summary>
    [RelayCommand]
    private async Task LoadFileAsync(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            StatusMessage = "文件不存在，请检查路径。";
            return;
        }
        await RunBusyAsync(async () =>
        {
            var client = SidecarClientProvider.Client;
            var detect = await client.DetectFormatAsync(path);
            if (!detect.Detected)
            {
                StatusMessage = "无适配器认领该文件的引擎格式。";
                return;
            }
            var envelope = await client.ExtractToIrAsync(path);
            _currentProject = envelope.Project;
            CurrentFilePath = path;
            Units.Clear();
            foreach (var unit in envelope.Project.Units)
            {
                Units.Add(new StudioUnitItemViewModel(unit));
            }
            SelectedUnit = Units.FirstOrDefault();
            UnitsView.Refresh();
            RefreshProgress();
            StatusMessage = $"已加载 {envelope.UnitCount} 条可译文本（引擎：{detect.Adapter}）。";
        });
    }

    /// <summary>
    /// 批量翻译：提取 EXTRACTED / LQA_FAILED 单元送翻并按 id 回写各行。
    /// 默认 API Base 指向本地环回 Mock（127.0.0.1:18080/v1），零额度离线演练。
    /// </summary>
    [RelayCommand]
    private async Task TranslateBatchAsync()
    {
        var targets = Units
            .Where(u => u.Status is "EXTRACTED" or "LQA_FAILED")
            .Select(u => u.Model)
            .ToList();
        if (targets.Count == 0)
        {
            StatusMessage = "没有待翻译单元（均已通过质检）。";
            return;
        }
        await RunBusyAsync(async () =>
        {
            StatusMessage = $"正在翻译 {targets.Count} 条…";
            var stopwatch = System.Diagnostics.Stopwatch.StartNew();
            var config = new TranslationConfigDto(
                ApiBase: TranslateApiBase,
                ModelName: ModelName);
            var updated = await SidecarClientProvider.Client.TranslateBatchAsync(targets, config);
            stopwatch.Stop();
            var byId = updated.ToDictionary(u => u.Id);
            foreach (var item in Units)
            {
                if (byId.TryGetValue(item.Model.Id, out var dto))
                {
                    item.Update(dto);
                }
            }
            RefreshProgress();
            LatencyText = $"Latency: {stopwatch.ElapsedMilliseconds:#,0}ms";
            StatusMessage =
                $"批次完成：{updated.Count(u => u.Status == "LQA_PASSED")} 通过，"
                + $"{updated.Count(u => u.Status == "LQA_FAILED")} 待返工"
                + $"（端点：{TranslateApiBase}）。";
        });
    }

    /// <summary>单条重试：仅重翻该行（Failed 行的 🔄 Re-try 快捷动作）。</summary>
    [RelayCommand]
    private async Task RetryUnitAsync(StudioUnitItemViewModel? item)
    {
        if (item is null)
        {
            return;
        }
        await RunBusyAsync(async () =>
        {
            var config = new TranslationConfigDto(
                ApiBase: TranslateApiBase,
                ModelName: ModelName);
            var updated = await SidecarClientProvider.Client.TranslateBatchAsync(
                [item.Model], config);
            foreach (var dto in updated)
            {
                Units.First(u => u.Model.Id == dto.Id).Update(dto);
            }
            RefreshProgress();
            StatusMessage = $"已重试单元 {item.Id}。";
        });
    }

    /// <summary>导出回写：以行 VM 的最新 Model 重建项目信封（既定教训：抽取期
    /// 快照不含批量译文与人工微调），经 ir_to_asset 原位还原为游戏脚本。</summary>
    [RelayCommand]
    private async Task ExportScriptAsync()
    {
        if (_currentProject is null)
        {
            StatusMessage = "尚未载入任何剧本，无法导出。";
            return;
        }
        await RunBusyAsync(async () =>
        {
            foreach (var unit in Units)
            {
                unit.CommitEdits();
            }
            var project = _currentProject with { Units = Units.Select(u => u.Model).ToList() };
            var result = await SidecarClientProvider.Client.IrToAssetAsync(project, ExportDirectory);
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
        PassedCount = passed;
        FailedCount = failed;
        ProgressPercent = total == 0 ? 0 : Math.Round((passed + failed) * 100.0 / total, 1);
    }

    /// <summary>情境审查抽屉数据：按单元 id 确定性生成（同单元同数据，可复现）。</summary>
    private void RebuildInspector(StudioUnitItemViewModel? unit)
    {
        WaveformBars.Clear();
        GlossaryItems.Clear();
        if (unit is null)
        {
            return;
        }
        var seed = unit.Id.Aggregate(17, (acc, c) => acc * 31 + c);
        var rng = new Random(seed);
        foreach (var height in Enumerable.Range(0, 28).Select(_ => rng.Next(8, 56)))
        {
            WaveformBars.Add(height);
        }
        foreach (var gloss in new[]
                 {
                     "学園祭 → 学园祭 [Verified]",
                     "生徒会 → 学生会 [Verified]",
                     "風紀 → 风纪委员 [Verified]",
                 })
        {
            GlossaryItems.Add(gloss);
        }
    }

    /// <summary>语音物理时长（确定性占位：1200~2600ms）。</summary>
    public string DurationLabel => SelectedUnit is null
        ? "—"
        : $"{1_200 + Math.Abs(SelectedUnit.Id.Aggregate(17, (acc, c) => acc * 31 + c)) % 1_400:#,0}ms";

    /// <summary>等待补偿微调指示（确定性占位：+0~+400ms）。</summary>
    public string CompLabel => SelectedUnit is null
        ? "—"
        : $"+{50 * (Math.Abs(SelectedUnit.Id.Aggregate(17, (acc, c) => acc * 31 + c)) % 9)}ms";

    public void Dispose() => _currentProject = null;
}
