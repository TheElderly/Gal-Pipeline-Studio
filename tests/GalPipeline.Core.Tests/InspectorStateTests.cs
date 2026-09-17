using System.Text.Json;
using GalPipeline.Core.Inspector;
using GalPipeline.Core.IPC;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// Inspector 行级联动的纯函数契约测试。
///
/// 覆盖需求里的两条核心断言：
///
/// 1. **切换 SelectedUnit 时 Inspector 派生状态正确** —— 逐字段比对具名角色 /
///    旁白 / 空角色三种行的完整快照；
/// 2. **有语音行与无语音行的分流** —— 播放控件启用态、时长胶囊、波形数据
///    （确定性包络 vs 平直静音基线）在两侧必须严格不同。
///
/// 之所以能在无 GUI 纪律下做到：派生逻辑全在 <c>GalPipeline.Core</c>（零 WPF 依赖），
/// 测试工程可以直接引用；壳层 VM 只做一层属性转发。
/// </summary>
public class InspectorStateTests
{
    // --------------------------- 构造助手 ---------------------------

    private static TranslationUnitDto Unit(
        string id,
        string? speaker,
        string? metadataJson = null,
        string status = "EXTRACTED")
        => new(
            Id: id,
            Speaker: speaker,
            RawText: "raw",
            ExtractedText: "text",
            AtomicTags: [],
            PairedTags: [],
            TranslatedText: null,
            Status: status,
            Metadata: metadataJson is null ? null : ParseMetadata(metadataJson));

    /// <summary>把一段 JSON 对象字面量解析成 metadata 字典（与 IPC 反序列化同形态）。</summary>
    private static Dictionary<string, JsonElement> ParseMetadata(string json)
    {
        using var document = JsonDocument.Parse(json);
        return document.RootElement.EnumerateObject()
            .ToDictionary(property => property.Name, property => property.Value.Clone());
    }

    // ===========================================================================
    // 1. 音频线索解析
    // ===========================================================================

    [Fact]
    public void AudioCue_MissingOrBlankMetadata_IsNull()
    {
        Assert.Null(AudioCue.FromMetadata(null));
        Assert.Null(AudioCue.FromMetadata(ParseMetadata("{}")));
        Assert.Null(AudioCue.FromMetadata(ParseMetadata("""{"audio": null}""")));
        Assert.Null(AudioCue.FromMetadata(ParseMetadata("""{"audio": ""}""")));
        Assert.Null(AudioCue.FromMetadata(ParseMetadata("""{"audio": "   "}""")));
        Assert.Null(AudioCue.FromMetadata(ParseMetadata("""{"audio": {}}""")));
        // 只有无法识别的键 → 不构成线索（避免把无关元数据误判成配音）
        Assert.Null(AudioCue.FromMetadata(ParseMetadata("""{"audio": {"unknown": 1}}""")));
    }

    [Fact]
    public void AudioCue_StringShorthand_KeepsFileNameWithoutDuration()
    {
        var cue = AudioCue.FromMetadata(ParseMetadata("""{"audio": "vo_00012.ogg"}"""));

        Assert.NotNull(cue);
        Assert.Equal("vo_00012.ogg", cue!.File);
        Assert.True(cue.HasAsset);
        Assert.Null(cue.DurationMs);
        Assert.Null(cue.CompMs);
    }

    [Fact]
    public void AudioCue_FullObject_ReadsFileDurationAndCompensation()
    {
        var cue = AudioCue.FromMetadata(ParseMetadata(
            """{"audio": {"file": "vo_00012.ogg", "duration_ms": 1850, "comp_ms": 200}}"""));

        Assert.NotNull(cue);
        Assert.Equal("vo_00012.ogg", cue!.File);
        Assert.Equal(1850, cue.DurationMs);
        Assert.Equal(200, cue.CompMs);
    }

    [Fact]
    public void AudioCue_VoiceKey_IsAcceptedAsAlias()
    {
        var cue = AudioCue.FromMetadata(ParseMetadata("""{"voice": "vo_alias.ogg"}"""));

        Assert.NotNull(cue);
        Assert.Equal("vo_alias.ogg", cue!.File);
    }

    [Fact]
    public void AudioCue_AudioKeyWinsOverVoiceKey()
    {
        var cue = AudioCue.FromMetadata(ParseMetadata(
            """{"voice": "old.ogg", "audio": "new.ogg"}"""));

        Assert.Equal("new.ogg", cue!.File);
    }

    [Fact]
    public void AudioCue_CamelCaseAndStringNumbers_AreTolerated()
    {
        var cue = AudioCue.FromMetadata(ParseMetadata(
            """{"audio": {"path": "vo.ogg", "durationMs": "1850", "comp": 400}}"""));

        Assert.NotNull(cue);
        Assert.Equal("vo.ogg", cue!.File);
        Assert.Equal(1850, cue.DurationMs);
        Assert.Equal(400, cue.CompMs);
    }

    [Fact]
    public void AudioCue_DurationWithoutFileName_StillCountsAsAudio()
    {
        // 适配器只报了时长、没报资源名：这仍说明「本行有配音」，只是暂时播不了。
        // 判成静音是错的 —— 那会让人以为这行本来就没配音。
        var cue = AudioCue.FromMetadata(ParseMetadata("""{"audio": {"duration_ms": 900}}"""));

        Assert.NotNull(cue);
        Assert.False(cue!.HasAsset);
        Assert.Equal(900, cue.DurationMs);
    }

    // ===========================================================================
    // 2. 波形采样
    // ===========================================================================

    [Fact]
    public void Waveform_Silence_IsFlatBaselineOfFixedLength()
    {
        var silence = WaveformSampler.Silence();

        Assert.Equal(WaveformSampler.BarCount, silence.Count);
        Assert.All(silence, height => Assert.Equal(WaveformSampler.SilenceHeight, height));
    }

    [Fact]
    public void Waveform_ForVoice_IsDeterministicAcrossCalls()
    {
        var first = WaveformSampler.ForVoice("vo_00012.ogg");
        var second = WaveformSampler.ForVoice("vo_00012.ogg");

        Assert.Equal(first, second);
        // 不同资源名 → 不同波形（否则「每行波形各异」的观感不成立）
        Assert.NotEqual(first, WaveformSampler.ForVoice("vo_00099.ogg"));
    }

    [Fact]
    public void Waveform_ForVoice_KeepsEveryBarVisible()
    {
        var bars = WaveformSampler.ForVoice("vo_00012.ogg");

        Assert.Equal(WaveformSampler.BarCount, bars.Count);
        Assert.All(bars, height => Assert.True(height >= 1, $"柱高 {height} 不可见"));
    }

    [Fact]
    public void Waveform_ForVoice_DecaysTowardBothEnds()
    {
        var bars = WaveformSampler.ForVoice("vo_00012.ogg");

        // 逐柱比较会被伪随机扰动偶尔打穿，故比较**分段均值**：
        // 首尾各 8 根的平均高度必须显著低于中段 16 根。
        var head = bars.Take(8).Average();
        var middle = bars.Skip(8).Take(16).Average();
        var tail = bars.Skip(24).Average();

        Assert.True(middle > head, $"中段 {middle:F1} 未高于首段 {head:F1}");
        Assert.True(middle > tail, $"中段 {middle:F1} 未高于尾段 {tail:F1}");
    }

    [Fact]
    public void Waveform_StableSeed_IgnoresHashRandomization()
    {
        // 同值同种子、空值与 null 均不得抛（空种子直接落在基准值 17）
        Assert.Equal(WaveformSampler.StableSeed("abc"), WaveformSampler.StableSeed("abc"));
        Assert.NotEqual(WaveformSampler.StableSeed("abc"), WaveformSampler.StableSeed("abd"));
        Assert.Equal(17, WaveformSampler.StableSeed(null));
        Assert.Equal(17, WaveformSampler.StableSeed(""));
    }

    // ===========================================================================
    // 3. 切换选中行 → 派生状态正确
    // ===========================================================================

    [Fact]
    public void Unselected_StateIsTheEmptyInspector()
    {
        var state = InspectorState.For(null);

        Assert.Equal(InspectorState.NoSelectionTitle, state.Title);
        Assert.False(state.HasSelection);
        Assert.False(state.HasVoice);
        Assert.False(state.IsPlaybackEnabled);
        Assert.Equal(InspectorState.UnknownDurationLabel, state.DurationLabel);
        Assert.Equal(InspectorState.NoCompensationLabel, state.CompLabel);
        Assert.Empty(state.GlossaryMatches);
        Assert.Equal(WaveformSampler.Silence(), state.Waveform);
    }

    [Fact]
    public void Title_SyncsWithSelectedUnitLineNumber()
    {
        Assert.Equal("Inspector #00142", InspectorState.For(Unit("sample_act1-00142", "千代")).Title);
        Assert.Equal("Inspector #00001", InspectorState.For(Unit("scene-00001", null)).Title);
        // 尾段非数字（异常 id）也要给出稳定的兜底，而不是抛异常
        Assert.Equal("Inspector #00000", InspectorState.For(Unit("weird-id", null)).Title);
        Assert.Equal(InspectorState.NoSelectionTitle, InspectorState.TitleFor(null));
    }

    [Fact]
    public void NamedSpeaker_UsesStandingCardWithoutPlaceholder()
    {
        var state = InspectorState.For(Unit("s-00001", "千代"));

        Assert.True(state.HasSelection);
        Assert.Equal("千代", state.SpeakerName);
        Assert.False(state.IsSpeakerPlaceholder);
        Assert.Equal(InspectorState.StandingCaption, state.CharacterCardCaption);
    }

    [Theory]
    [InlineData(null)]           // 空角色
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("Narrator")]     // 需求点名的占位触发词
    [InlineData("narrator")]
    [InlineData("旁白")]
    [InlineData("ナレーション")]
    public void NarrationLikeSpeaker_EntersPlaceholderState(string? speaker)
    {
        var state = InspectorState.For(Unit("s-00002", speaker));

        Assert.True(state.IsSpeakerPlaceholder);
        Assert.Equal(InspectorState.PlaceholderCaption, state.CharacterCardCaption);
        // 空角色归一到「旁白」；具名的 Narrator 保留原字面量（不丢信息）
        Assert.Equal(string.IsNullOrWhiteSpace(speaker) ? InspectorState.NarratorLabel : speaker.Trim(),
            state.SpeakerName);
    }

    [Fact]
    public void SwitchingSelection_ProducesDistinctStatesPerRow()
    {
        // 三行覆盖全部分支：具名+有配音 / 旁白+无配音 / 具名+无配音
        var voiced = InspectorState.For(
            Unit("sample_act1-00012", "千代",
                """{"audio": {"file": "vo_00012.ogg", "duration_ms": 1850, "comp_ms": 200}}"""),
            [new GlossaryMatch("真実", "真相", "Verified")]);

        var narration = InspectorState.For(Unit("sample_act1-00014", null));
        var silentNamed = InspectorState.For(Unit("sample_act1-00017", "主人公"));

        // 标题逐行不同
        Assert.Equal("Inspector #00012", voiced.Title);
        Assert.Equal("Inspector #00014", narration.Title);
        Assert.Equal("Inspector #00017", silentNamed.Title);

        // 角色卡三分支
        Assert.Equal("差分卡 · STANDING", voiced.CharacterCardCaption);
        Assert.Equal("旁白 · NO SPRITE", narration.CharacterCardCaption);
        Assert.Equal("差分卡 · STANDING", silentNamed.CharacterCardCaption);

        // 音频卡分流：只有 voiced 行可用
        Assert.True(voiced.IsPlaybackEnabled);
        Assert.False(narration.IsPlaybackEnabled);
        Assert.False(silentNamed.IsPlaybackEnabled);

        Assert.Equal("1,850ms", voiced.DurationLabel);
        Assert.Equal("--:--", narration.DurationLabel);
        Assert.Equal("--:--", silentNamed.DurationLabel);

        Assert.Equal("+200ms", voiced.CompLabel);
        Assert.Equal("—", narration.CompLabel);

        // 术语 Chip 只挂在有命中的那行
        Assert.Equal(["真実 → 真相 [Verified]"], voiced.GlossaryMatches);
        Assert.Empty(narration.GlossaryMatches);
    }

    // ===========================================================================
    // 4. 有语音 / 无语音的波形数据分流
    // ===========================================================================

    [Fact]
    public void VoicedRow_ProducesEnvelopeWaveform()
    {
        var state = InspectorState.For(
            Unit("s-00010", "千代", """{"audio": {"file": "vo_00010.ogg", "duration_ms": 1850}}"""));

        Assert.True(state.HasVoice);
        Assert.True(state.IsPlaybackEnabled);
        Assert.Equal(WaveformSampler.BarCount, state.Waveform.Count);
        Assert.Equal(WaveformSampler.ForVoice("vo_00010.ogg"), state.Waveform);
        Assert.NotEqual(WaveformSampler.Silence(), state.Waveform);
    }

    [Fact]
    public void SilentRow_ProducesFlatBaselineAndDisabledPlayback()
    {
        var state = InspectorState.For(Unit("s-00011", "千代"));

        Assert.False(state.HasVoice);
        Assert.False(state.IsPlaybackEnabled);
        Assert.Equal(WaveformSampler.Silence(), state.Waveform);
        Assert.Equal(InspectorState.UnknownDurationLabel, state.DurationLabel);
        Assert.Equal(InspectorState.NoCompensationLabel, state.CompLabel);
    }

    [Fact]
    public void VoicedRowWithoutDuration_StillEnablesPlaybackButHidesDuration()
    {
        // 有资源可播、但时长未知 —— 播放键**要**亮（能播），时长如实显示 --:--
        var state = InspectorState.For(Unit("s-00012", "千代", """{"audio": "vo_00012.ogg"}"""));

        Assert.True(state.HasVoice);
        Assert.True(state.IsPlaybackEnabled);
        Assert.Equal(InspectorState.UnknownDurationLabel, state.DurationLabel);
        Assert.NotEqual(WaveformSampler.Silence(), state.Waveform);
    }

    [Fact]
    public void WaveformSeedFollowsVoiceAsset_NotUnitOrder()
    {
        // 同一段语音挂在不同行 → 同一条波形（避免「换行就换脸」）
        var later = InspectorState.For(
            Unit("s-00099", "千代", """{"audio": {"file": "vo_shared.ogg"}}"""));
        var earlier = InspectorState.For(
            Unit("s-00001", "千代", """{"audio": {"file": "vo_shared.ogg"}}"""));

        Assert.Equal(earlier.Waveform, later.Waveform);
    }

    // ===========================================================================
    // 5. 术语 Chip 格式化
    // ===========================================================================

    [Fact]
    public void GlossaryChips_AreFormattedAndOrderedAsGiven()
    {
        var glossary = new List<GlossaryMatch>
        {
            new("指切りげんまん", "拉钩上吊", "Verified"),
            new("針千本", "一千根针", "Verified"),
        };

        var state = InspectorState.For(Unit("s-00027", "千代"), glossary);

        Assert.Equal(
            ["指切りげんまん → 拉钩上吊 [Verified]", "針千本 → 一千根针 [Verified]"],
            state.GlossaryMatches);
    }

    [Fact]
    public void GlossaryChip_FallsBackWhenNoteIsMissing()
    {
        Assert.Equal("約束 → 约定 [Verified]", new GlossaryMatch("約束", "约定").Chip);
        Assert.Equal("約束 → 约定 [Verified]", new GlossaryMatch("約束", "约定", "").Chip);
        Assert.Equal("約束 → 约定 [Auto]", new GlossaryMatch("約束", "约定", "Auto").Chip);
    }

    [Fact]
    public void NullGlossary_YieldsEmptyChips()
    {
        Assert.Empty(InspectorState.For(Unit("s-00001", "千代"), null).GlossaryMatches);
        Assert.Empty(InspectorState.For(Unit("s-00001", "千代"), []).GlossaryMatches);
    }
}
