using GalPipeline.Core.IPC;

namespace GalPipeline.Core.Inspector;

/// <summary>
/// 一条术语命中（同时充当 JSON-RPC 的 <c>match_glossary</c> 响应元素 ——
/// IPC 客户端已全局启用 <c>SnakeCaseLower</c>，字段名天然对齐 Python 侧）。
/// </summary>
public sealed record GlossaryMatch(string Source, string Target, string Note = "Verified")
{
    /// <summary>缺乏来源标注时的兜底（机器抽取条目应由后端显式标注）。</summary>
    public const string DefaultNote = "Verified";

    public string NoteLabel => string.IsNullOrWhiteSpace(Note) ? DefaultNote : Note.Trim();

    /// <summary>术语 Chip 的显示文案：<c>真実 → 真相 [Verified]</c>。</summary>
    public string Chip => $"{Source?.Trim()} → {Target?.Trim()} [{NoteLabel}]";
}

/// <summary>
/// Inspector（右侧情境审查抽屉）的**完整派生状态** —— 纯函数求值，零依赖、零副作用。
///
/// 为什么要单独抽这一层：Inspector 是「行级联动」最密集的区域，标题、角色卡占位、
/// 音频卡启用态、波形数据、术语 Chip 全部随 <c>SelectedUnit</c> 切换而变。
/// 而壳层 VM 属 <c>net10.0-windows</c>，<c>dotnet test</c> 工程（<c>net10.0</c>，无 UseWPF）
/// **引用不了它** —— 派生逻辑留在 VM 里就只能靠肉眼点行验证。下沉到此，
/// 「切换 SelectedUnit 时 Inspector 状态是否正确」才成为可自动化的断言。
///
/// 不可变：本对象一经构造即为该单元的快照，切换行 = 换一个新快照，
/// 不存在「半新半旧」的中间态。
/// </summary>
public sealed record InspectorState
{
    /// <summary>无选中行时的抽屉标题。</summary>
    public const string NoSelectionTitle = "Context Inspector";

    /// <summary>无名说话人的显示名（旁白）。</summary>
    public const string NarratorLabel = "旁白";

    /// <summary>具名角色的差分卡标注。</summary>
    public const string StandingCaption = "差分卡 · STANDING";

    /// <summary>旁白/无名角色的差分卡占位标注。</summary>
    public const string PlaceholderCaption = "旁白 · NO SPRITE";

    /// <summary>无配音（或时长未知）时的时长胶囊文案。</summary>
    public const string UnknownDurationLabel = "--:--";

    /// <summary>无补偿偏移时的胶囊文案（用长破折号，与时长胶囊的 "--:--" 区分语义）。</summary>
    public const string NoCompensationLabel = "—";

    /// <summary>
    /// 被视为「非具名角色」的说话人字面量（大小写不敏感，按整词比较）。
    /// 命中即进入占位态：角色卡不给立绘承诺，改为标注 NO SPRITE。
    /// </summary>
    private static readonly string[] NarrationSpeakers =
        ["narrator", "narration", "旁白", "ナレーション"];

    public required string Title { get; init; }

    /// <summary>是否有选中的行（false 时抽屉显示空态提示，其余字段为占位值）。</summary>
    public required bool HasSelection { get; init; }

    /// <summary>角色卡上显示的说话人名（无名时为「旁白」）。</summary>
    public required string SpeakerName { get; init; }

    /// <summary>是否处于角色占位态（旁白 / Narrator / 空角色）。</summary>
    public required bool IsSpeakerPlaceholder { get; init; }

    /// <summary>角色卡底部标注（具名 → 差分卡 · STANDING；占位 → 旁白 · NO SPRITE）。</summary>
    public required string CharacterCardCaption { get; init; }

    /// <summary>本行是否有配音线索。</summary>
    public required bool HasVoice { get; init; }

    /// <summary>播放三键是否可用（= <see cref="HasVoice"/>，独立暴露以便将来区分「有资源但不可播」）。</summary>
    public required bool IsPlaybackEnabled { get; init; }

    /// <summary>时长胶囊文案（<c>1,850ms</c> / <c>--:--</c>）。</summary>
    public required string DurationLabel { get; init; }

    /// <summary>补偿偏移胶囊文案（<c>+200ms</c> / <c>—</c>）。</summary>
    public required string CompLabel { get; init; }

    /// <summary>波形柱高序列：有配音走确定性包络，无配音走平直静音基线。</summary>
    public required IReadOnlyList<int> Waveform { get; init; }

    /// <summary>术语 Chip 文案列表（<c>源 → 译 [标注]</c>），无命中时为空。</summary>
    public required IReadOnlyList<string> GlossaryMatches { get; init; }

    /// <summary>无选中行时的空态快照（抽屉被折叠，但保持一个合法状态便于断言）。</summary>
    public static InspectorState Unselected { get; } = new()
    {
        Title = NoSelectionTitle,
        HasSelection = false,
        SpeakerName = NarratorLabel,
        IsSpeakerPlaceholder = true,
        CharacterCardCaption = PlaceholderCaption,
        HasVoice = false,
        IsPlaybackEnabled = false,
        DurationLabel = UnknownDurationLabel,
        CompLabel = NoCompensationLabel,
        Waveform = WaveformSampler.Silence(),
        GlossaryMatches = [],
    };

    /// <summary>
    /// 由单元（可空）与术语命中集合派生出整份 Inspector 状态。
    /// <paramref name="unit"/> 为 <c>null</c> 时返回 <see cref="Unselected"/>。
    /// </summary>
    public static InspectorState For(
        TranslationUnitDto? unit,
        IReadOnlyList<GlossaryMatch>? glossary = null)
    {
        if (unit is null)
        {
            return Unselected;
        }

        var hasSpeaker = !string.IsNullOrWhiteSpace(unit.Speaker);
        var speaker = hasSpeaker ? unit.Speaker!.Trim() : NarratorLabel;
        var isPlaceholder = !hasSpeaker || IsNarrationSpeaker(unit.Speaker);

        var cue = AudioCue.FromMetadata(unit.Metadata);
        var hasVoice = cue is not null;

        return new InspectorState
        {
            Title = TitleFor(unit.Id),
            HasSelection = true,
            SpeakerName = speaker,
            IsSpeakerPlaceholder = isPlaceholder,
            CharacterCardCaption = isPlaceholder ? PlaceholderCaption : StandingCaption,
            HasVoice = hasVoice,
            IsPlaybackEnabled = hasVoice,
            DurationLabel = FormatDuration(cue),
            CompLabel = FormatCompensation(cue),
            // 波形 seed 优先取音频资源名：同一段语音无论挂在哪一行都长同一条波形；
            // 资源名缺失时退回单元 id，保证「有配音」的行彼此仍可区分。
            Waveform = hasVoice
                ? WaveformSampler.ForVoice(cue!.File ?? unit.Id)
                : WaveformSampler.Silence(),
            GlossaryMatches = FormatGlossary(glossary),
        };
    }

    /// <summary>抽屉标题：<c>Inspector #00142</c>；无 id 时为 <see cref="NoSelectionTitle"/>。</summary>
    public static string TitleFor(string? unitId)
        => string.IsNullOrWhiteSpace(unitId) ? NoSelectionTitle : $"Inspector {LineNumberTag(unitId)}";

    /// <summary>
    /// 行号标签：取单元 id 的尾段序号并补零为 <c>#00142</c>。
    /// 契约里单元 id 形如 <c>sample_act1-00012</c>，尾段即源脚本行号。
    /// </summary>
    public static string LineNumberTag(string unitId)
    {
        var dash = unitId.LastIndexOf('-');
        return dash >= 0 && int.TryParse(unitId[(dash + 1)..], out var number)
            ? $"#{number:00000}"
            : "#00000";
    }

    /// <summary>说话人是否属于「非具名角色」（旁白 / Narrator / 空）。</summary>
    public static bool IsNarrationSpeaker(string? speaker)
    {
        if (string.IsNullOrWhiteSpace(speaker))
        {
            return true;
        }
        var trimmed = speaker.Trim();
        return NarrationSpeakers.Any(
            candidate => string.Equals(candidate, trimmed, StringComparison.OrdinalIgnoreCase));
    }

    private static string FormatDuration(AudioCue? cue)
    {
        if (cue?.DurationMs is not { } duration)
        {
            return UnknownDurationLabel;
        }
        // 手工定型而非插值：# 与 , 会随区域性变化（fr-FR 用不换行空格作千位分隔），
        // 文案一旦漂移，设计稿对齐与自动化断言都会失效。
        return duration.ToString("N0", System.Globalization.CultureInfo.InvariantCulture) + "ms";
    }

    private static string FormatCompensation(AudioCue? cue)
        => cue?.CompMs is { } comp
            ? "+" + comp.ToString("N0", System.Globalization.CultureInfo.InvariantCulture) + "ms"
            : NoCompensationLabel;

    private static IReadOnlyList<string> FormatGlossary(IReadOnlyList<GlossaryMatch>? glossary)
        => glossary is null || glossary.Count == 0
            ? []
            : glossary.Select(match => match.Chip).ToArray();
}
