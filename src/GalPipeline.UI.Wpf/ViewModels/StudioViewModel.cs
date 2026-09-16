using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows.Data;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;
using Microsoft.Win32;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// Studio 剧本工坊视图模型：载入 → 双栏审校（筛选/搜索/批量翻译/LQA 门禁/
/// 人工微调/单条重试）→ 导出回写；右栏情境审查抽屉。
/// 启动即注入内置演示数据，保证首屏表格饱满（对照设计稿）。
/// </summary>
public sealed partial class StudioViewModel : ObservableObject, IDisposable
{
    public const string DefaultMockApiBase = "http://127.0.0.1:18080/v1";

    private GalIRProjectDto? _currentProject;

    /// <summary>指标通报口：MainWindow 注入后，Studio 各项指标实时汇入主窗底栏。</summary>
    public static Action<string, string>? MetricsSink;

    public ObservableCollection<StudioUnitItemViewModel> Units { get; } = [];

    public ICollectionView UnitsView { get; }

    [ObservableProperty]
    public partial string CurrentFilePath { get; set; } = "demo_scenario.ks";

    [ObservableProperty]
    public partial string TranslateApiBase { get; set; } = DefaultMockApiBase;

    [ObservableProperty]
    public partial string ModelName { get; set; } = "glm-4-flash";

    [ObservableProperty]
    public partial bool IsBusy { get; set; }

    [ObservableProperty]
    public partial string StatusMessage { get; set; } = "就绪。";

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
    public partial string LatencyText { get; set; } = "Latency: 180ms";

    [ObservableProperty]
    public partial string TokenUsageText { get; set; } = "Session Tokens: 142.5k";

    public ObservableCollection<int> WaveformBars { get; } = [];

    public ObservableCollection<string> GlossaryItems { get; } = [];

    public bool HasSelectedUnit => SelectedUnit is not null;

    /// <summary>抽屉标题：选行联动（Inspector #00142 / 默认 Context Inspector）。</summary>
    public string InspectorTitle => SelectedUnit is null
        ? "Context Inspector"
        : $"Inspector {SelectedUnit.LineNumberTag}";

    /// <summary>抽屉宽度：展开 280 / 折叠 32（停靠条，常驻再展开把手）。</summary>
    public double InspectorWidth => IsInspectorOpen ? 280 : 32;

    public System.Windows.Visibility InspectorContentVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorCollapseVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorExpandVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Collapsed : System.Windows.Visibility.Visible;

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
    partial void OnStatusMessageChanged(string value) => PublishMetrics();

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
        OnPropertyChanged(nameof(InspectorWidth));
        OnPropertyChanged(nameof(InspectorContentVisibility));
        OnPropertyChanged(nameof(InspectorCollapseVisibility));
        OnPropertyChanged(nameof(InspectorExpandVisibility));
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
        OnPropertyChanged(nameof(InspectorTitle));
        RebuildInspector(value);
    }

    /// <summary>抽屉折叠开关。</summary>
    [RelayCommand]
    private void ToggleInspector() => IsInspectorOpen = !IsInspectorOpen;

    public StudioViewModel()
    {
        UnitsView = CollectionViewSource.GetDefaultView(Units);
        UnitsView.Filter = FilterUnit;

        // 默认注入演示数据：彻底解决开局中央表格空态塌陷（对照设计稿首屏）
        LoadDemoUnits();
    }

    /// <summary>内置演示行：正常对白 / 宏标签行 / 1 条 Quote mismatch 的 LQA_FAILED。</summary>
    private void LoadDemoUnits()
    {
        Units.Clear();
        var rows = new (int Id, string Speaker, string Source, string Target, string? Macro, string Status, string? Issue)[]
        {
            (142, "Akira", "僕の返事はもう決まっていたはずです。", "我的回答原本早就已经定好了。", null, "LQA_FAILED", "Quote mismatch: Missing closed quote"),
            (143, "Akira", "昨日のこと、本当に楽しかったよね。", "昨天的事情，真的很开心呢。", "wait:300ms", "LQA_PASSED", null),
            (144, "Akira", "空を見上げると、一面の星空が広がっていた。", "仰望天空，满天繁星若隐若现。", null, "LQA_PASSED", null),
            (145, "Mei", "どうしてそんな寂しい顔をするの？", "为什么露出那么落寞的表情？", "wait:300ms", "LQA_PASSED", null),
            (146, "Mei", "約束したよね、必ず帰ってくるって。", "明明约定过，一定会回来的。", null, "LQA_PASSED", null),
            (147, "Narrator", "風が二人の間を静かに吹き抜けていく。", "微风悄无声息地穿过二人之间。", "wait:300ms", "LQA_PASSED", null),
            (148, "Akira", "ああ、覚えているよ。", "啊啊，我还记得呢。", null, "LQA_PASSED", null),
            (149, "Mei", "本当？嘘ついたら怒るからね。", "真的？要是撒谎我可是会生气的。", "wait:300ms", "LQA_PASSED", null),
            (150, "Akira", "絶対に嘘なんてつかないさ。", "我绝对不会撒谎的。", null, "EXTRACTED", null),
            (151, "Narrator", "彼女の手が、そっと僕の指先に触れた。", "她的手，轻轻触碰到了我的指尖。", "wait:300ms", "EXTRACTED", null),
            (152, "Akira", "温かい……これが現実なんだ。", "好温暖……这就是现实啊。", null, "EXTRACTED", null),
            (153, "Mei", "行こう、みんなが待っている場所へ。", "走吧，去往大家都在等待的地方。", "wait:300ms", "EXTRACTED", null),
        };

        foreach (var row in rows)
        {
            List<AtomicTagDto> tags = string.IsNullOrEmpty(row.Macro)
                ? []
                : [new AtomicTagDto(TagId: "macro", RawTag: $"<{row.Macro}>", Position: 0)];

            Dictionary<string, JsonElement>? metadata = null;
            if (!string.IsNullOrEmpty(row.Issue))
            {
                using var doc = JsonDocument.Parse(
                    $"[{{\"severity\":\"error\",\"message\":\"{row.Issue}\"}}]");
                metadata = new Dictionary<string, JsonElement>
                {
                    ["lqa_issues"] = doc.RootElement.Clone(),
                };
            }

            Units.Add(new StudioUnitItemViewModel(new TranslationUnitDto(
                Id: $"unit-{row.Id:00000}",
                Speaker: row.Speaker,
                RawText: row.Source,
                ExtractedText: row.Source,
                AtomicTags: [.. tags],
                PairedTags: [],
                TranslatedText: row.Target,
                Status: row.Status,
                Metadata: metadata)));
        }

        SelectedUnit = Units.FirstOrDefault();
        RefreshProgress();
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

    /// <summary>打开文件选择器（*.ks / *.txt），选定后走统一载入链。</summary>
    [RelayCommand]
    private async Task OpenFilePickerAsync()
    {
        var dialog = new OpenFileDialog
        {
            Filter = "Galgame Script (*.ks;*.txt)|*.ks;*.txt|All files (*.*)|*.*",
            Title = "选择剧本文件",
        };
        if (dialog.ShowDialog() == true)
        {
            await LoadFileAsync(dialog.FileName);
        }
    }

    /// <summary>载入剧本（公开命令，支持文件选择器与拖放复用）。</summary>
    [RelayCommand]
    public async Task LoadFileAsync(string? path)
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
            PublishMetrics();
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
            StatusMessage = "没有待翻译单元。";
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
            PublishMetrics();
            StatusMessage = $"批次完成：{updated.Count(u => u.Status == "LQA_PASSED")} 通过。";
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

    /// <summary>导出回写：以行 VM 的最新 Model 重建项目信封（既定教训），经
    /// ir_to_asset 原位还原为游戏脚本；Demo 数据无真实工程信封，须先载入文件。</summary>
    [RelayCommand]
    private async Task ExportScriptAsync()
    {
        if (_currentProject is null)
        {
            StatusMessage = "当前为演示数据，请先通过「打开」载入外部剧本。";
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
        PublishMetrics();
    }

    private void PublishMetrics() =>
        MetricsSink?.Invoke(FileStatusLabel, $"{TokenUsageText} · {LatencyText}");

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
        foreach (var height in Enumerable.Range(0, 28).Select(_ => rng.Next(10, 48)))
        {
            WaveformBars.Add(height);
        }
        GlossaryItems.Add("学園祭 → 学园祭 [Verified]");
        GlossaryItems.Add("生徒会 → 学生会 [Verified]");
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
