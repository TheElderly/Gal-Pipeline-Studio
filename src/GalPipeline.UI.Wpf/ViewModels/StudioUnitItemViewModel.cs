using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using CommunityToolkit.Mvvm.ComponentModel;
using GalPipeline.Core.Inspector;
using GalPipeline.Core.IPC;
using GalPipeline.Core.Lqa;
using GalPipeline.Core.Translation;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>单个宏标记药丸（深紫色只读胶囊的绑定载体）。</summary>
public sealed record MacroPill(string Text);

/// <summary>
/// Project Explorer 工程树的剧本节点绑定载体（P1 多剧本联动）。
/// 数据来自共享层 <see cref="GalPipeline.Core.Project.ScriptWorkspace"/> 的扫描结果。
/// </summary>
public sealed record ScriptFileNode(string FullPath, string RelativePath, long SizeBytes)
{
    /// <summary>树节点显示名：相对路径 —— 同名剧本在不同子目录下仍可区分。</summary>
    public string DisplayName => RelativePath;
}

/// <summary>
/// Studio 审校行视图模型：包装 TranslationUnitDto，四列呈现
/// （行号+角色徽章 / 日文原文+宏药丸 / 中文译文行内编辑 / LQA 状态）。
/// </summary>
public sealed partial class StudioUnitItemViewModel : ObservableObject
{
    // 状态胶囊：**饱和实心填充 + 纯白文字**（对照设计稿 LQA 徽章的视觉强度）
    private static readonly Brush PassedBrush = new SolidColorBrush(Color.FromRgb(0x2E, 0x9E, 0x4F));
    private static readonly Brush FailedBrush = new SolidColorBrush(Color.FromRgb(0xC0, 0x39, 0x2B));
    private static readonly Brush DraftBrush = new SolidColorBrush(Color.FromRgb(0x3A, 0x3A, 0x3C));
    private static readonly Brush PassedForeground = new SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF));
    private static readonly Brush FailedForeground = new SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF));
    private static readonly Brush DraftForeground = new SolidColorBrush(Color.FromRgb(0xFF, 0xFF, 0xFF));

    // 状态胶囊 Keyline（同色系微亮描边，40% alpha）：实心填充下仅做轮廓收边，不再承担辨识职责
    private static readonly Brush PassedKeyline = new SolidColorBrush(Color.FromArgb(0x66, 0x6F, 0xD0, 0x8C));
    private static readonly Brush FailedKeyline = new SolidColorBrush(Color.FromArgb(0x66, 0xE0, 0x70, 0x5F));
    private static readonly Brush DraftKeyline = new SolidColorBrush(Color.FromArgb(0x66, 0x6A, 0x6A, 0x70));

    /// <summary>徽章文字：高对比冷白（对齐设计稿 #EEF8FF），暗底上保持可读性。</summary>
    private static readonly Brush BadgeColdWhite = new SolidColorBrush(Color.FromRgb(0xEE, 0xF8, 0xFF));

    /// <summary>
    /// 角色徽章底色：设计稿的「低饱和暗钢蓝微阶」（基准众数 #35455A）。
    /// 仅保留极小的明度/色相差异用于区分讲者——彻底废弃高饱和高光色块。
    /// </summary>
    private static readonly Brush[] SpeakerPalette =
    [
        new SolidColorBrush(Color.FromRgb(0x35, 0x45, 0x5A)),
        new SolidColorBrush(Color.FromRgb(0x2F, 0x40, 0x54)),
        new SolidColorBrush(Color.FromRgb(0x3A, 0x4D, 0x63)),
        new SolidColorBrush(Color.FromRgb(0x2B, 0x3A, 0x4D)),
        new SolidColorBrush(Color.FromRgb(0x40, 0x54, 0x68)),
    ];

    public TranslationUnitDto Model { get; private set; }

    public StudioUnitItemViewModel(TranslationUnitDto model)
    {
        Model = model;
        TranslatedText = model.TranslatedText ?? string.Empty;
        Status = model.Status;
    }

    public string Id => Model.Id;

    /// <summary>等宽行号标签：取单元 id 尾段序号，格式 #00012（与 Inspector 标题同源）。</summary>
    public string LineNumberTag => InspectorState.LineNumberTag(Model.Id);

    /// <summary>
    /// 说话人显示名（无名 → 旁白）。委托 <see cref="InspectorState.SpeakerName"/>：
    /// 该值同时被搜索过滤与徽章使用，必须与 Inspector 角色卡上的写法完全一致。
    /// </summary>
    public string Speaker => Inspector.SpeakerName;

    /// <summary>角色徽章文案：方括号包裹的 [Speaker]（搜索过滤仍走 Speaker 原始值，勿改动）。</summary>
    public string SpeakerBadge => $"[{Speaker}]";

    /// <summary>角色徽章底色：按角色名确定性取色（同名同色，跨会话稳定）。</summary>
    public Brush SpeakerBadgeBrush
    {
        get
        {
            var hash = Speaker.Aggregate(17, (acc, c) => acc * 31 + c);
            return SpeakerPalette[Math.Abs(hash) % SpeakerPalette.Length];
        }
    }

    /// <summary>角色徽章文字色（冷白，配合低饱和暗底）。</summary>
    public Brush SpeakerBadgeForeground => BadgeColdWhite;

    public string SourceText => Model.ExtractedText;

    /// <summary>日文原文明细中的宏药丸胶囊（逐字面，深紫样式由 XAML 承载）。</summary>
    public IReadOnlyList<MacroPill> MacroPills =>
        Model.AtomicTags.Select(t => new MacroPill(t.RawTag)).ToList();

    public int MacroCount => Model.AtomicTags.Count;

    public string MacroSummary =>
        Model.AtomicTags.Count == 0 ? "—" : string.Join(" ", Model.AtomicTags.Select(t => t.RawTag));

    public bool IsFailed => Status == "LQA_FAILED";

    /// <summary>已通过：LQA_PASSED，或已导出的终态（EXPORTED = 质检通过 + 已落盘）。</summary>
    public bool IsPassed => Status is "LQA_PASSED" or "EXPORTED";

    /// <summary>已导出：闭环终点标记（TranslationStatus.EXPORTED 在壳层的唯一落点）。</summary>
    public bool IsExported => Status == "EXPORTED";

    /// <summary>待处理：既未通过也未失败、且尚未导出（EXPORTED 不得计入待译，否则进度回退）。</summary>
    public bool IsPending => Status is not ("LQA_PASSED" or "LQA_FAILED" or "EXPORTED");

    /// <summary>
    /// 待翻译：已抽取（EXTRACTED）且**译文为空白**。
    ///
    /// 判定公式与统计层共用同一份实现（<see cref="TranslationProgress.NeedsTranslation"/>），
    /// 杜绝「按钮角标计数」与「批量选取目标」两处口径漂移 —— 二者一旦不一致，
    /// 就会出现「显示待译 3 条、点下去报没有待翻译单元」这类静默错位。
    /// 取的是 <see cref="TranslatedText"/>（编辑缓冲）而非 Model：用户刚敲进去、
    /// 尚未失焦提交的译文也算数，否则批量会把它覆盖掉。
    /// </summary>
    public bool NeedsTranslation => TranslationProgress.NeedsTranslation(Status, TranslatedText);

    public Brush StatusBrush =>
        IsFailed ? FailedBrush : IsPassed ? PassedBrush : DraftBrush;

    public Brush StatusForeground =>
        IsFailed ? FailedForeground : IsPassed ? PassedForeground : DraftForeground;

    /// <summary>胶囊 1px 半透明 Keyline 描边（Fluent 2 胶囊轮廓，随状态确定性取色）。</summary>
    public Brush StatusKeylineBrush =>
        IsFailed ? FailedKeyline : IsPassed ? PassedKeyline : DraftKeyline;

    /// <summary>胶囊文案（严禁裸露后端枚举）：✓ Passed / ⊗ Failed / Draft。</summary>
    public string StatusDisplay =>
        IsPassed ? "✓ Passed" : IsFailed ? "⊗ Failed" : "Draft";

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

    /// <summary>
    /// 本行全部违例留痕（rule_id / severity / message），LQA Lab 看板的聚合输入。
    /// metadata 缺失或畸形项静默跳过 —— 看板只承载有效明细。
    /// </summary>
    public IReadOnlyList<LqaIssueLine> GetIssues()
    {
        if (Model.Metadata is not { } metadata
            || !metadata.TryGetValue("lqa_issues", out var issues)
            || issues.ValueKind is not JsonValueKind.Array)
        {
            return [];
        }
        var lines = new List<LqaIssueLine>();
        foreach (var issue in issues.EnumerateArray())
        {
            if (issue.ValueKind is not JsonValueKind.Object)
            {
                continue;
            }
            var rule = issue.TryGetProperty("rule_id", out var r) ? r.GetString() : null;
            var severity = issue.TryGetProperty("severity", out var s) ? s.GetString() : null;
            var message = issue.TryGetProperty("message", out var m) ? m.GetString() : null;
            if (!string.IsNullOrWhiteSpace(rule)
                && !string.IsNullOrWhiteSpace(severity)
                && !string.IsNullOrWhiteSpace(message))
            {
                lines.Add(new LqaIssueLine(rule, severity, message));
            }
        }
        return lines;
    }

    // ------------------------------------------------------------------
    // Inspector 行级联动：全部派生自 Model（说话人 / 音频元数据）与术语命中，
    // 判定与格式化一律走共享层 GalPipeline.Core.Inspector，壳层不重复实现。
    // ------------------------------------------------------------------

    private IReadOnlyList<GlossaryMatch> _glossary = [];
    private InspectorState? _inspector;

    /// <summary>Inspector 的整份派生快照（不可变；Model 替换或术语到位时失效重算）。</summary>
    public InspectorState Inspector => _inspector ??= InspectorState.For(Model, _glossary);

    public string InspectorTitle => Inspector.Title;

    /// <summary>角色卡上的说话人名（与 <see cref="Speaker"/> 同源）。</summary>
    public string SpeakerName => Inspector.SpeakerName;

    /// <summary>角色占位态：旁白 / Narrator / 空角色（XAML 据此把角色名调暗）。</summary>
    public bool IsSpeakerPlaceholder => Inspector.IsSpeakerPlaceholder;

    public string CharacterCardCaption => Inspector.CharacterCardCaption;

    /// <summary>本行是否有配音线索（<c>metadata.audio</c> / <c>metadata.voice</c>）。</summary>
    public bool HasVoice => Inspector.HasVoice;

    /// <summary>播放三键可用性：无配音即置灰。</summary>
    public bool IsPlaybackEnabled => Inspector.IsPlaybackEnabled;

    /// <summary>时长胶囊：<c>1,850ms</c> / 无配音或时长未知时为 <c>--:--</c>。</summary>
    public string DurationLabel => Inspector.DurationLabel;

    /// <summary>补偿偏移胶囊：<c>+200ms</c> / 未知时为 <c>—</c>。</summary>
    public string CompLabel => Inspector.CompLabel;

    /// <summary>波形柱高序列：有配音走确定性包络，无配音为平直静音基线。</summary>
    public IReadOnlyList<int> Waveform => Inspector.Waveform;

    /// <summary>术语 Chip 文案集合（<c>源 → 译 [标注]</c>），随行切换动态增减。</summary>
    public IReadOnlyList<string> GlossaryMatches => Inspector.GlossaryMatches;

    public bool HasGlossaryMatches => GlossaryMatches.Count > 0;

    /// <summary>术语卡空态提示：无命中时给一句话，而不是留一张只有标题的空卡片。</summary>
    public Visibility GlossaryEmptyVisibility =>
        HasGlossaryMatches ? Visibility.Collapsed : Visibility.Visible;

    /// <summary>
    /// Inspector 派生属性的统一通知清单 —— 失效时一次广播，避免新增派生属性后漏通知。
    /// 其中 <c>Speaker</c> / <c>SpeakerBadge</c> / <c>SpeakerBadgeBrush</c> / <c>LineNumberTag</c>
    /// 也已改为委托 Inspector，故必须一并列入（漏掉会出现「卡片变了、徽章没变」）。
    /// </summary>
    private static readonly string[] InspectorDerived =
    [
        nameof(Inspector),
        nameof(InspectorTitle),
        nameof(SpeakerName),
        nameof(IsSpeakerPlaceholder),
        nameof(CharacterCardCaption),
        nameof(HasVoice),
        nameof(IsPlaybackEnabled),
        nameof(DurationLabel),
        nameof(CompLabel),
        nameof(Waveform),
        nameof(GlossaryMatches),
        nameof(HasGlossaryMatches),
        nameof(GlossaryEmptyVisibility),
        nameof(Speaker),
        nameof(SpeakerBadge),
        nameof(SpeakerBadgeBrush),
        nameof(LineNumberTag),
    ];

    /// <summary>
    /// 装备术语命中。载入时整表批量取回后逐行分发；可重复调用（重载同一文件即刷新）。
    /// </summary>
    public void ApplyGlossary(IReadOnlyList<GlossaryMatch>? matches)
    {
        _glossary = matches ?? [];
        InvalidateInspector();
    }

    /// <summary>
    /// 丢弃 Inspector 快照并广播。**只在 Model 被整体替换或术语到位时调用**：
    /// <see cref="CommitEdits"/>（只改译文）与 <see cref="MarkExported"/>（只改状态）
    /// 都不影响 Inspector 读到的字段，为它们付 17 次通知是纯浪费。
    /// </summary>
    private void InvalidateInspector()
    {
        _inspector = null;
        foreach (var name in InspectorDerived)
        {
            OnPropertyChanged(name);
        }
    }

    [ObservableProperty]
    public partial string TranslatedText { get; set; }

    [ObservableProperty]
    public partial string Status { get; set; }

    partial void OnStatusChanged(string value)
    {
        OnPropertyChanged(nameof(StatusBrush));
        OnPropertyChanged(nameof(StatusForeground));
        OnPropertyChanged(nameof(StatusKeylineBrush));
        OnPropertyChanged(nameof(StatusDisplay));
        OnPropertyChanged(nameof(IsFailed));
        OnPropertyChanged(nameof(IsPassed));
        OnPropertyChanged(nameof(IsExported));
        OnPropertyChanged(nameof(IsPending));
        OnPropertyChanged(nameof(NeedsTranslation));
        OnPropertyChanged(nameof(IssueTooltip));
        OnPropertyChanged(nameof(HasIssue));
    }

    /// <summary>译文编辑缓冲变化 → 待翻译判定随之变化（批量角标与进度条靠它实时衰减）。</summary>
    partial void OnTranslatedTextChanged(string value) =>
        OnPropertyChanged(nameof(NeedsTranslation));

    /// <summary>
    /// 导出成功后把本行推进 EXPORTED 终态（契约状态机
    /// ``LQA_PASSED → EXPORTED`` 的实际落点，此前全仓库无写入）。
    /// 幂等：已导出则直接返回，避免重复触发无谓的通知风暴。
    /// </summary>
    public void MarkExported()
    {
        if (Model.Status == "EXPORTED")
        {
            return;
        }
        Model = Model with { Status = "EXPORTED" };
        Status = "EXPORTED";
    }

    /// <summary>以 Sidecar 回传的 DTO 原子化刷新本行（批量翻译完成后调用）。</summary>
    public void Update(TranslationUnitDto dto)
    {
        Model = dto;
        // Model 整体替换：说话人 / 音频元数据 / id 都可能随新 DTO 变化，Inspector 必须失效
        InvalidateInspector();
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
