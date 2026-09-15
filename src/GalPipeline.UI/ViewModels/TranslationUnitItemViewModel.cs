using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using Microsoft.UI.Xaml.Media;
using Windows.UI;
using GalPipeline.Core.IPC;

namespace GalPipeline.UI.ViewModels;

/// <summary>单个剧本条目的行视图模型：包装 DTO 并承载可编辑译文与状态徽章。</summary>
public sealed partial class TranslationUnitItemViewModel : ObservableObject
{
    public TranslationUnitDto Model { get; private set; }

    public TranslationUnitItemViewModel(TranslationUnitDto model)
    {
        Model = model;
        TranslatedText = model.TranslatedText ?? string.Empty;
        Status = model.Status;
    }

    public string Id => Model.Id;
    public string Speaker => string.IsNullOrEmpty(Model.Speaker) ? "旁白" : Model.Speaker;
    public bool HasSpeaker => !string.IsNullOrEmpty(Model.Speaker);
    public string SourceText => Model.ExtractedText;
    public bool IsFailed => Status == "LQA_FAILED";
    public bool IsPassed => Status == "LQA_PASSED";
    public bool IsPending => Status is not ("LQA_PASSED" or "LQA_FAILED");
    public bool HasWarnings => WarningCount > 0;

    private static readonly Brush PassedBrush = new SolidColorBrush(Color.FromArgb(255, 0x46, 0xB3, 0x5B));
    private static readonly Brush FailedBrush = new SolidColorBrush(Color.FromArgb(255, 0xE0, 0x4F, 0x5F));
    private static readonly Brush PendingBrush = new SolidColorBrush(Color.FromArgb(255, 0x8A, 0x8A, 0x8A));

    /// <summary>状态徽章底色：供经典 Binding 数据模板消费（规避模板内 x:Bind 编译器缺陷）。</summary>
    public Brush StatusBrush => IsFailed ? FailedBrush : IsPassed ? PassedBrush : PendingBrush;

    public string WarningLabel => WarningCount > 0 ? $"⚠ {WarningCount} 条提示" : string.Empty;

    public Microsoft.UI.Xaml.Visibility SpeakerVisibility =>
        HasSpeaker ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    [ObservableProperty]
    public partial string TranslatedText { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string Status { get; set; } = string.Empty;

    partial void OnStatusChanged(string value)
    {
        OnPropertyChanged(nameof(IsFailed));
        OnPropertyChanged(nameof(IsPassed));
        OnPropertyChanged(nameof(IsPending));
        OnPropertyChanged(nameof(StatusBrush));
        OnPropertyChanged(nameof(WarningLabel));
        OnPropertyChanged(nameof(SpeakerVisibility));
    }

    /// <summary>以 Sidecar 回传的 DTO 原子化刷新本行（翻译批次完成后调用）。</summary>
    public void Update(TranslationUnitDto dto)
    {
        Model = dto;
        TranslatedText = dto.TranslatedText ?? string.Empty;
        Status = dto.Status;
        OnPropertyChanged(nameof(WarningCount));
        OnPropertyChanged(nameof(HasWarnings));
    }

    /// <summary>用户手动微调译文后回写进 DTO，保证导出与回灌使用最新译文。</summary>
    public void CommitEdits()
    {
        var text = string.IsNullOrWhiteSpace(TranslatedText) ? null : TranslatedText;
        if (Model.TranslatedText != text)
        {
            Model = Model with { TranslatedText = text };
        }
    }

    /// <summary>从 metadata["lqa_issues"] 统计 warning 级提示数（error 已由 FAILED 徽章表达）。</summary>
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
}
