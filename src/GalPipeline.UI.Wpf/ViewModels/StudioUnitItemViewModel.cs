using System.Text.Json;
using System.Windows.Media;
using CommunityToolkit.Mvvm.ComponentModel;
using GalPipeline.Core.IPC;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// Studio 审校行视图模型：包装 TranslationUnitDto，双栏呈现
/// （左：说话人 + 原文与可编辑译文；右：宏标记摘要 + LQA 状态）。
/// </summary>
public sealed partial class StudioUnitItemViewModel : ObservableObject
{
    private static readonly Brush PassedBrush = new SolidColorBrush(Color.FromRgb(0x46, 0xB3, 0x5B));
    private static readonly Brush FailedBrush = new SolidColorBrush(Color.FromRgb(0xE0, 0x4F, 0x5F));
    private static readonly Brush PendingBrush = new SolidColorBrush(Color.FromRgb(0x8A, 0x8A, 0x8A));

    public TranslationUnitDto Model { get; private set; }

    public StudioUnitItemViewModel(TranslationUnitDto model)
    {
        Model = model;
        TranslatedText = model.TranslatedText ?? string.Empty;
        Status = model.Status;
    }

    public string Id => Model.Id;
    public string Speaker => string.IsNullOrWhiteSpace(Model.Speaker) ? "旁白" : Model.Speaker;
    public bool HasSpeaker => !string.IsNullOrWhiteSpace(Model.Speaker);

    /// <summary>旁白行隐藏说话人列（System.Windows.Visibility 供经典 Binding 消费）。</summary>
    public System.Windows.Visibility SpeakerVisibility =>
        HasSpeaker ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public string SourceText => Model.ExtractedText;

    /// <summary>右栏宏标记摘要：逐字面列出（空表显示占位符）。</summary>
    public string MacroSummary =>
        Model.AtomicTags.Count == 0 ? "—" : string.Join("\n", Model.AtomicTags.Select(t => t.RawTag));

    public int MacroCount => Model.AtomicTags.Count;

    public bool IsFailed => Status == "LQA_FAILED";
    public bool IsPassed => Status == "LQA_PASSED";
    public bool IsPending => Status is not ("LQA_PASSED" or "LQA_FAILED");

    public Brush StatusBrush => IsFailed ? FailedBrush : IsPassed ? PassedBrush : PendingBrush;

    [ObservableProperty]
    public partial string TranslatedText { get; set; }

    [ObservableProperty]
    public partial string Status { get; set; }

    partial void OnStatusChanged(string value) => OnPropertyChanged(nameof(StatusBrush));

    /// <summary>以 Sidecar 回传的 DTO 原子化刷新本行（批量翻译完成后调用）。</summary>
    public void Update(TranslationUnitDto dto)
    {
        Model = dto;
        TranslatedText = dto.TranslatedText ?? string.Empty;
        Status = dto.Status;
        OnPropertyChanged(nameof(MacroSummary));
        OnPropertyChanged(nameof(WarningLabel));
    }

    /// <summary>人工微调后回写 DTO，保证导出取到最新译文（与 WinUI 端纪律一致）。</summary>
    public void CommitEdits()
    {
        var text = string.IsNullOrWhiteSpace(TranslatedText) ? null : TranslatedText;
        if (Model.TranslatedText != text)
        {
            Model = Model with { TranslatedText = text };
        }
    }

    /// <summary>metadata["lqa_issues"] 中 warning 级提示数（error 已由红徽章表达）。</summary>
    public int WarningCount
    {
        get
        {
            if (Model.Metadata is not { } metadata
                || !metadata.TryGetValue("lqa_issues", out var issues)
                || issues.ValueKind is not JsonValueKind.Array)
            {
                return 0;
            }
            return issues.EnumerateArray().Count(i =>
                i.ValueKind is JsonValueKind.Object
                && i.TryGetProperty("severity", out var severity)
                && severity.ValueKind is JsonValueKind.String
                && severity.GetString() == "warning");
        }
    }

    public string WarningLabel => WarningCount > 0 ? $"⚠ {WarningCount} 条提示" : string.Empty;
}
