using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using CommunityToolkit.Mvvm.ComponentModel;
using GalPipeline.Core.IPC;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>单个宏标记药丸（深紫色只读胶囊的绑定载体）。</summary>
public sealed record MacroPill(string Text);

/// <summary>
/// Studio 审校行视图模型：包装 TranslationUnitDto，四列呈现
/// （行号+角色徽章 / 日文原文+宏药丸 / 中文译文行内编辑 / LQA 状态）。
/// </summary>
public sealed partial class StudioUnitItemViewModel : ObservableObject
{
    private static readonly Brush PassedBrush = new SolidColorBrush(Color.FromRgb(0x2E, 0xA0, 0x4E));
    private static readonly Brush FailedBrush = new SolidColorBrush(Color.FromRgb(0xD9, 0x3F, 0x4B));
    private static readonly Brush PendingBrush = new SolidColorBrush(Color.FromRgb(0x8A, 0x8A, 0x8A));

    private static readonly Brush[] SpeakerPalette =
    [
        new SolidColorBrush(Color.FromRgb(0x4C, 0x8D, 0xBF)),
        new SolidColorBrush(Color.FromRgb(0xBF, 0x6E, 0x4C)),
        new SolidColorBrush(Color.FromRgb(0x8A, 0x5C, 0xBF)),
        new SolidColorBrush(Color.FromRgb(0x4C, 0xBF, 0x8D)),
        new SolidColorBrush(Color.FromRgb(0xBF, 0x4C, 0x8D)),
    ];

    public TranslationUnitDto Model { get; private set; }

    public StudioUnitItemViewModel(TranslationUnitDto model)
    {
        Model = model;
        TranslatedText = model.TranslatedText ?? string.Empty;
        Status = model.Status;
    }

    public string Id => Model.Id;

    /// <summary>等宽行号标签：取单元 id 尾段序号，格式 #00012。</summary>
    public string LineNumberTag
    {
        get
        {
            var dash = Model.Id.LastIndexOf('-');
            return dash >= 0 && int.TryParse(Model.Id[(dash + 1)..], out var number)
                ? $"#{number:00000}"
                : "#00000";
        }
    }

    public string Speaker => string.IsNullOrWhiteSpace(Model.Speaker) ? "旁白" : Model.Speaker;

    /// <summary>角色徽章底色：按角色名确定性取色（同名同色，跨会话稳定）。</summary>
    public Brush SpeakerBadgeBrush
    {
        get
        {
            var hash = Speaker.Aggregate(17, (acc, c) => acc * 31 + c);
            return SpeakerPalette[Math.Abs(hash) % SpeakerPalette.Length];
        }
    }

    public string SourceText => Model.ExtractedText;

    /// <summary>日文原文明细中的宏药丸胶囊（逐字面，深紫样式由 XAML 承载）。</summary>
    public IReadOnlyList<MacroPill> MacroPills =>
        Model.AtomicTags.Select(t => new MacroPill(t.RawTag)).ToList();

    public int MacroCount => Model.AtomicTags.Count;

    public string MacroSummary =>
        Model.AtomicTags.Count == 0 ? "—" : string.Join(" ", Model.AtomicTags.Select(t => t.RawTag));

    public bool IsFailed => Status == "LQA_FAILED";
    public bool IsPassed => Status == "LQA_PASSED";
    public bool IsPending => Status is not ("LQA_PASSED" or "LQA_FAILED");

    public Brush StatusBrush => IsFailed ? FailedBrush : IsPassed ? PassedBrush : PendingBrush;

    /// <summary>首条 error 级违例消息（Failed 行的异常气泡 Tooltip）。</summary>
    public string? IssueTooltip
    {
        get
        {
            if (Model.Metadata is not { } metadata
                || !metadata.TryGetValue("lqa_issues", out var issues)
                || issues.ValueKind is not JsonValueKind.Array)
            {
                return null;
            }
            return issues.EnumerateArray()
                .Where(i => i.ValueKind is JsonValueKind.Object
                            && i.TryGetProperty("severity", out var s)
                            && s.ValueKind is JsonValueKind.String
                            && s.GetString() == "error")
                .Select(i => i.TryGetProperty("message", out var m) ? m.GetString() : null)
                .FirstOrDefault(m => !string.IsNullOrWhiteSpace(m));
        }
    }

    public bool HasIssue => !string.IsNullOrWhiteSpace(IssueTooltip);

    [ObservableProperty]
    public partial string TranslatedText { get; set; }

    [ObservableProperty]
    public partial string Status { get; set; }

    partial void OnStatusChanged(string value)
    {
        OnPropertyChanged(nameof(StatusBrush));
        OnPropertyChanged(nameof(IsFailed));
        OnPropertyChanged(nameof(IsPassed));
        OnPropertyChanged(nameof(IsPending));
        OnPropertyChanged(nameof(IssueTooltip));
        OnPropertyChanged(nameof(HasIssue));
    }

    /// <summary>以 Sidecar 回传的 DTO 原子化刷新本行（批量翻译完成后调用）。</summary>
    public void Update(TranslationUnitDto dto)
    {
        Model = dto;
        TranslatedText = dto.TranslatedText ?? string.Empty;
        Status = dto.Status;
        OnPropertyChanged(nameof(MacroPills));
        OnPropertyChanged(nameof(MacroSummary));
        OnPropertyChanged(nameof(WarningLabel));
    }

    /// <summary>人工微调后回写 DTO，保证导出取到最新译文。</summary>
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
