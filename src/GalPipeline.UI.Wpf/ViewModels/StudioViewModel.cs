using System.Collections.ObjectModel;
using System.IO;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// Studio 剧本工坊视图模型：载入剧本（detect + extract）→ 双栏审校
/// （批量翻译 + LQA 门禁 + 人工微调）→ 供导出回写。
/// 批量翻译默认指向本地环回 Mock（零额度离线演练），可改为真实端点。
/// </summary>
public sealed partial class StudioViewModel : ObservableObject, IDisposable
{
    /// <summary>本地环回 Mock 端点：tests/fixtures/mock_llm_server.py。</summary>
    public const string DefaultMockApiBase = "http://127.0.0.1:18080/v1";

    private GalIRProjectDto? _currentProject;

    public ObservableCollection<StudioUnitItemViewModel> Units { get; } = [];

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

    public bool IsIdle => !IsBusy;

    public int PendingCount => TotalCount - PassedCount - FailedCount;

    public string ProgressLabel =>
        $"已通过 {PassedCount} / 待翻译 {PendingCount} / 不合格 {FailedCount}，共 {TotalCount} 条";

    partial void OnIsBusyChanged(bool value) => OnPropertyChanged(nameof(IsIdle));
    partial void OnTotalCountChanged(int value) => OnPropertyChanged(nameof(ProgressLabel));
    partial void OnPassedCountChanged(int value) => OnPropertyChanged(nameof(ProgressLabel));
    partial void OnFailedCountChanged(int value) => OnPropertyChanged(nameof(ProgressLabel));

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
            var config = new TranslationConfigDto(
                ApiBase: TranslateApiBase,
                ModelName: ModelName);
            var updated = await SidecarClientProvider.Client.TranslateBatchAsync(targets, config);
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
                + $"{updated.Count(u => u.Status == "LQA_FAILED")} 待返工"
                + $"（端点：{TranslateApiBase}）。";
        });
    }

    [ObservableProperty]
    public partial string ExportDirectory { get; set; } = "tests/fixtures/output";

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
            // 纵深防御：不依赖 LostFocus 时序，导出前强制全量同步人工微调
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

    public void Dispose() => _currentProject = null;
}
