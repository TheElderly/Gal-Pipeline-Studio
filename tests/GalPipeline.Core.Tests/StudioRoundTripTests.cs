using System.Text;
using System.Text.Json;
using GalPipeline.Core.IPC;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 真实业务闭环的 IPC 层端到端测试：**不经 Mock**，直接驱动真实样本
/// <c>tests/fixtures/sample_act1.ks</c> 走「载入 → 审校 → 导出 → 再抽取」全链路。
///
/// 覆盖三层契约：
///
/// 1. 抽取契约 —— 单元 id / 说话人 / 宏登记必须与真实剧本逐条对应；
/// 2. 无损回写 —— 未改动译文时回写产物与源文件内容字节一致；人工改动后
///    骨架行（注释 / 跳转标签 / @ 命令）仍逐字节原样，宏原位守恒；
/// 3. 即时质检 —— <c>run_lqa</c> 必须拦下人工改坏的译文，修复后放行并清除
///    陈旧违例留痕，清空译文则回归待译态。
///
/// 刻意不引用 WPF 项目：本测试工程是 <c>net10.0</c>（无 UseWPF），
/// 引用 <c>StudioViewModel</c> 会编译失败。故这里测的是 IPC 契约本身，
/// 壳层 VM 的行为由 tests/test_xaml_binding_contract.py 静态核对兜底。
/// </summary>
public class StudioRoundTripTests
{
    private static readonly string RepoRoot = LocateRepositoryRoot();
    private static readonly string SamplePath = Path.Combine(RepoRoot, "tests", "fixtures", "sample_act1.ks");

    private static readonly string[] ExpectedUnitIds =
    [
        "sample_act1-00012", "sample_act1-00014", "sample_act1-00015", "sample_act1-00017",
        "sample_act1-00018", "sample_act1-00025", "sample_act1-00027", "sample_act1-00028",
    ];

    private static readonly string?[] ExpectedSpeakers =
        ["千代", null, null, "主人公", "主人公", null, "千代", "主人公"];

    private static readonly string[][] ExpectedMacros =
    [
        ["[ruby text=\"しんじつ\"]", "[p]"],
        ["[r]"],
        ["[font size=24]", "[font size=default]", "[p]"],
        ["[ruby text=\"ちよ\"]", "[r]"],
        ["[ruby text=\"まこと\"]", "[p]"],
        ["[p]"],
        ["[ruby text=\"ゆびきり\"]", "[r]", "[p]"],
        ["[p]"],
    ];

    private static readonly string[] ManualTranslations =
    [
        "「知道真相的觉悟，早就已经做好了吧？」",
        "我回想起的，是那个雨夜的事。",
        "被打折了伞的我，就那样浑身湿透地站在家门之前。",
        "「那个时候，千代什么也没有说。」",
        "「只是选择了贯彻诚实这一条路。」",
        "只有波浪的声音，在两人之间流淌着。",
        "「拉钩上吊，说谎的话就吞一千根针。」",
        "「……那个约定，我至今仍然守着。」",
    ];

    /// <summary>骨架行（1-based 行号 → 原文）：注释 / 跳转标签 / @ 命令，回写后必须逐字节原样。</summary>
    private static readonly Dictionary<int, string> SkeletonLines = new()
    {
        [1] = "; ============================================================",
        [7] = "*scene_start",
        [8] = "@bg storage=\"room.png\" time=800",
        [9] = "@wait time=400",
        [11] = "; ---- 第一幕：真相 ----",
        [20] = "; ---- 第二幕：夜の海岸 ----",
        [21] = "*scene_coast",
        [22] = "@bg storage=\"coast_night.png\" time=1200",
        [23] = "@cm",
        [30] = "*scene_end",
    };

    [Fact]
    public async Task SampleScript_IsPresentAndStructurallyValid()
    {
        Assert.True(File.Exists(SamplePath), $"内置真实样本缺失：{SamplePath}");
        var raw = await File.ReadAllBytesAsync(SamplePath);
        Assert.True(raw.Length > 200 && raw[0] == 0xEF && raw[1] == 0xBB && raw[2] == 0xBF,
            "样本必须是带 UTF-8 BOM 的真实 KAG 剧本");
    }

    [Fact]
    public async Task DetectAndExtract_RealSample_YieldsRegisteredUnitsAndMacros()
    {
        using var client = new PythonSidecarClient();

        var detection = await client.DetectFormatAsync(SamplePath);
        Assert.True(detection.Detected);
        Assert.Equal("kag", detection.Adapter);

        var extracted = await client.ExtractToIrAsync(SamplePath);
        Assert.Equal(8, extracted.UnitCount);
        Assert.Equal(8, extracted.Stats.Total);
        Assert.Equal(8, extracted.Stats.ByStatus["EXTRACTED"]);

        Assert.Equal(ExpectedUnitIds, extracted.Project.Units.Select(u => u.Id).ToArray());
        Assert.Equal(ExpectedSpeakers, extracted.Project.Units.Select(u => u.Speaker).ToArray());

        for (var i = 0; i < ExpectedMacros.Length; i++)
        {
            Assert.Equal(ExpectedMacros[i], extracted.Project.Units[i].AtomicTags.Select(t => t.RawTag).ToArray());
        }

        // 宏不是文本装饰：抽取后正文里不得残留任何宏字面量
        Assert.All(extracted.Project.Units, unit =>
            Assert.DoesNotContain("[", unit.ExtractedText, StringComparison.Ordinal));
        Assert.Equal("「真実を知る覚悟は、もうできているの？」", extracted.Project.Units[0].ExtractedText);
    }

    [Fact]
    public async Task PristineRoundTrip_IsContentByteIdentical()
    {
        using var client = new PythonSidecarClient();
        var outputDir = Path.Combine(Path.GetTempPath(), $"roundtrip-{Guid.NewGuid():N}");
        try
        {
            var extracted = await client.ExtractToIrAsync(SamplePath);
            var exported = await client.IrToAssetAsync(extracted.Project, outputDir);

            var expected = Normalize(await File.ReadAllBytesAsync(SamplePath));
            var actual = Normalize(await File.ReadAllBytesAsync(exported.OutputPath));

            Assert.Equal(expected, actual);
        }
        finally
        {
            Cleanup(outputDir);
        }
    }

    [Fact]
    public async Task ManualEditRoundTrip_PreservesSkeletonMacrosAndTranslations()
    {
        using var client = new PythonSidecarClient();
        var outputDir = Path.Combine(Path.GetTempPath(), $"manual-{Guid.NewGuid():N}");
        try
        {
            var extracted = await client.ExtractToIrAsync(SamplePath);

            // 模拟人工内联审校：逐行覆写译文（真实链路是 LostFocus → CommitEdits）
            var edited = extracted.Project.Units
                .Zip(ManualTranslations, (unit, translation) => unit with { TranslatedText = translation })
                .ToList();

            // 即时质检：8 条人工译文必须全部通过，才允许进入导出
            var checkedUnits = await client.RunLqaAsync(edited);
            Assert.All(checkedUnits, unit => Assert.Equal("LQA_PASSED", unit.Status));

            var project = extracted.Project with { Units = checkedUnits };
            var exported = await client.IrToAssetAsync(project, outputDir);
            var rendered = Normalize(await File.ReadAllBytesAsync(exported.OutputPath)).Split('\n');

            // 骨架逐字节原样
            foreach (var (lineNo, literal) in SkeletonLines)
            {
                Assert.Equal(literal, rendered[lineNo - 1]);
            }

            // 宏原位守恒 + 译文落地：剥离宏后应恰好是「说话人前缀 + 译文」
            for (var i = 0; i < ExpectedUnitIds.Length; i++)
            {
                var unit = checkedUnits[i];
                var line = rendered[(int)unit.Metadata!["kag_line"].GetInt64() - 1];
                var body = ExpectedMacros[i].Aggregate(line, (acc, macro) => acc.Replace(macro, string.Empty));
                var prefix = string.IsNullOrEmpty(unit.Speaker) ? string.Empty : $"【{unit.Speaker}】";
                Assert.Equal(prefix + ManualTranslations[i], body);
            }

            // 再抽取：汉化产物必须还原出完全相同的人工译文与宏登记（幂等闭环）
            var reextracted = await client.ExtractToIrAsync(exported.OutputPath);
            Assert.Equal(ExpectedUnitIds, reextracted.Project.Units.Select(u => u.Id).ToArray());
            Assert.Equal(ExpectedSpeakers, reextracted.Project.Units.Select(u => u.Speaker).ToArray());
            Assert.Equal(ManualTranslations, reextracted.Project.Units.Select(u => u.ExtractedText).ToArray());
            for (var i = 0; i < ExpectedMacros.Length; i++)
            {
                Assert.Equal(ExpectedMacros[i],
                    reextracted.Project.Units[i].AtomicTags.Select(t => t.RawTag).ToArray());
            }
        }
        finally
        {
            Cleanup(outputDir);
        }
    }

    [Fact]
    public async Task RunLqaAsync_UnbalancedQuote_BlocksAndRecordsError()
    {
        using var client = new PythonSidecarClient();
        var broken = MakeProbeUnit("「约定是为了遵守而存在的。");

        var gated = await client.RunLqaAsync([broken]);
        var unit = Assert.Single(gated);

        Assert.Equal("LQA_FAILED", unit.Status);
        Assert.True(HasIssue(unit.Metadata, "error"));
        Assert.Equal("cjk_punctuation", FirstIssueRuleId(unit.Metadata, "error"));
    }

    [Fact]
    public async Task RunLqaAsync_Repaired_ClearsStaleIssues()
    {
        using var client = new PythonSidecarClient();
        var broken = MakeProbeUnit("「约定是为了遵守而存在的。");

        var damaged = await client.RunLqaAsync([broken]);
        Assert.Equal("LQA_FAILED", Assert.Single(damaged).Status);

        var repaired = broken with { TranslatedText = "「约定是为了遵守而存在的。」" };
        var repairedUnits = await client.RunLqaAsync([repaired]);
        var unit = Assert.Single(repairedUnits);

        Assert.Equal("LQA_PASSED", unit.Status);
        Assert.True(unit.Metadata is null || !unit.Metadata.ContainsKey("lqa_issues"),
            "违例修复后必须清除陈旧留痕，否则前台会残留错误气泡");
    }

    [Fact]
    public async Task RunLqaAsync_ClearedTranslation_ReturnsToExtracted()
    {
        using var client = new PythonSidecarClient();
        var cleared = MakeProbeUnit(null);

        var unit = Assert.Single(await client.RunLqaAsync([cleared]));

        Assert.Equal("EXTRACTED", unit.Status);
    }

    private static TranslationUnitDto MakeProbeUnit(string? translated) => new(
        Id: "gate-00001",
        Speaker: "千代",
        RawText: "【千代】「約束」は、守るためにある。",
        ExtractedText: "「約束」は、守るためにある。",
        AtomicTags: [],
        PairedTags: [],
        TranslatedText: translated,
        Status: "EXTRACTED",
        Metadata: null);

    private static bool HasIssue(Dictionary<string, JsonElement>? metadata, string severity)
        => FirstIssueRuleId(metadata, severity) is not null;

    private static string? FirstIssueRuleId(Dictionary<string, JsonElement>? metadata, string severity)
    {
        if (metadata is null
            || !metadata.TryGetValue("lqa_issues", out var issues)
            || issues.ValueKind is not JsonValueKind.Array)
        {
            return null;
        }
        return issues.EnumerateArray()
            .Where(i => i.ValueKind is JsonValueKind.Object
                        && i.TryGetProperty("severity", out var s)
                        && s.ValueKind is JsonValueKind.String
                        && s.GetString() == severity)
            .Select(i => i.TryGetProperty("rule_id", out var r) ? r.GetString() : null)
            .FirstOrDefault(r => !string.IsNullOrEmpty(r));
    }

    /// <summary>BOM 剥离 + 行尾归一为 LF：跨平台可比的「内容字节」形态。</summary>
    private static string Normalize(byte[] raw)
    {
        var text = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false).GetString(raw);
        if (text.StartsWith('\uFEFF'))
        {
            text = text[1..];
        }
        return text.Replace("\r\n", "\n").Replace('\r', '\n');
    }

    private static void Cleanup(string directory)
    {
        if (Directory.Exists(directory))
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    private static string LocateRepositoryRoot()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "core", "server", "rpc.py")))
            {
                return directory.FullName;
            }
            directory = directory.Parent;
        }
        throw new InvalidOperationException("未找到仓库根：缺少 core/server/rpc.py 锚点");
    }
}
