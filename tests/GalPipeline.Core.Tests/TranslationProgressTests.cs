using System.Globalization;
using GalPipeline.Core.Translation;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 翻译进度统计的纯函数契约测试。
///
/// 这些公式是全流程里最容易「看起来对、算起来错」的一环：终态行不得计入待译、
/// 空白译文不得计入已译、空表不得除零、进度文案不得随区域性漂移。
/// 壳层 <c>StudioViewModel</c> 属于 net10.0-windows，本测试工程无法引用它，
/// 故公式被下沉到共享库 —— 这里测的就是按钮角标、批量选取、进度条三处的**同一份**实现。
/// </summary>
public class TranslationProgressTests
{
    // --------------------------- 待翻译判定 ---------------------------

    [Theory]
    [InlineData("EXTRACTED", null)]
    [InlineData("EXTRACTED", "")]
    [InlineData("EXTRACTED", "   ")]
    [InlineData("EXTRACTED", "\t\n")]
    public void NeedsTranslation_ExtractedWithoutText_IsTrue(string status, string? text)
        => Assert.True(TranslationProgress.NeedsTranslation(status, text));

    [Theory]
    [InlineData("EXTRACTED", "用户刚敲进去的译文")]  // 编辑缓冲非空即视为已翻，批量不得覆盖
    [InlineData("LQA_PASSED", "通过")]
    [InlineData("LQA_FAILED", "失败但有译文")]        // 重翻失败行属单行 Re-try 职责
    [InlineData("LQA_FAILED", null)]                  // 携带有效诊断，不计入待译
    [InlineData("EXPORTED", "已导出")]
    [InlineData("RAW", null)]
    [InlineData(null, null)]
    [InlineData("", "")]
    public void NeedsTranslation_OtherwiseIsFalse(string? status, string? text)
        => Assert.False(TranslationProgress.NeedsTranslation(status, text));

    [Theory]
    [InlineData("译文")]
    [InlineData("  x  ")]
    public void HasTranslation_NonBlank_IsTrue(string text)
        => Assert.True(TranslationProgress.HasTranslation(text));

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData(" \t\n ")]
    public void HasTranslation_Blank_IsFalse(string? text)
        => Assert.False(TranslationProgress.HasTranslation(text));

    // --------------------------- 聚合与衰减 ---------------------------

    [Fact]
    public void Summarize_EmptyTable_ConvergesWithoutDivideByZero()
    {
        var snapshot = TranslationProgress.Summarize([]);

        Assert.Equal(0, snapshot.Total);
        Assert.Equal(0.0, snapshot.Ratio);              // 不得为 NaN
        Assert.Equal(0, snapshot.Percent);
        Assert.Equal("0% Translated", snapshot.Text);
        Assert.False(double.IsNaN(snapshot.Ratio));
    }

    [Fact]
    public void Summarize_FreshlyExtractedScript_IsAllPending()
    {
        // 真实样本的初始态：8 行全部 EXTRACTED + 译文空白
        var rows = Enumerable.Repeat<(string?, string?)>(("EXTRACTED", null), 8);
        var snapshot = TranslationProgress.Summarize(rows);

        Assert.Equal(8, snapshot.Total);
        Assert.Equal(8, snapshot.Pending);
        Assert.Equal(0, snapshot.Translated);
        Assert.Equal(0, snapshot.Passed);
        Assert.Equal(0, snapshot.Failed);
        Assert.Equal(0.0, snapshot.Ratio);
        Assert.Equal("0% Translated", snapshot.Text);
    }

    [Fact]
    public void Summarize_PartiallyTranslated_ReportsFractionAndPendingDecay()
    {
        // 3 行已通过 + 5 行仍待译（衰减公式的正确性锚点）
        var rows = new List<(string?, string?)>();
        rows.AddRange(Enumerable.Repeat<(string?, string?)>(("LQA_PASSED", "译文"), 3));
        rows.AddRange(Enumerable.Repeat<(string?, string?)>(("EXTRACTED", null), 5));

        var snapshot = TranslationProgress.Summarize(rows);

        Assert.Equal(8, snapshot.Total);
        Assert.Equal(3, snapshot.Translated);
        Assert.Equal(5, snapshot.Pending);
        Assert.Equal(3, snapshot.Passed);
        Assert.Equal(0.375, snapshot.Ratio, 12);
        Assert.Equal(38, snapshot.Percent);                  // 37.5 → 38（四舍五入远离零）
        Assert.Equal("38% Translated", snapshot.Text);
        Assert.Equal(snapshot.Total, snapshot.Translated + snapshot.Pending); // 恒等式
    }

    [Fact]
    public void Summarize_AllTranslated_ClosesOutPending()
    {
        var rows = Enumerable.Repeat<(string?, string?)>(("LQA_PASSED", "译文"), 6);
        var snapshot = TranslationProgress.Summarize(rows);

        Assert.Equal(0, snapshot.Pending);
        Assert.Equal(1.0, snapshot.Ratio);
        Assert.Equal(100, snapshot.Percent);
        Assert.Equal("100% Translated", snapshot.Text);
    }

    [Fact]
    public void Summarize_FailedRowStillCountsAsTranslatedButNotPending()
    {
        // 质检失败不等于没翻过：进度条应推进，但失败计数独立表达
        var rows = new (string?, string?)[]
        {
            ("LQA_PASSED", "通过"),
            ("LQA_FAILED", "「未配平"),
            ("EXTRACTED", null),
        };
        var snapshot = TranslationProgress.Summarize(rows);

        Assert.Equal(3, snapshot.Total);
        Assert.Equal(2, snapshot.Translated);
        Assert.Equal(1, snapshot.Pending);
        Assert.Equal(1, snapshot.Passed);
        Assert.Equal(1, snapshot.Failed);
    }

    [Fact]
    public void Summarize_ExportedCountsAsPassedAndNotPending()
    {
        // EXPORTED 是闭环终态：计入已通过（否则进度回退），且绝不计入待译
        var rows = new (string?, string?)[]
        {
            ("EXPORTED", "已落盘"),
            ("EXTRACTED", null),
        };
        var snapshot = TranslationProgress.Summarize(rows);

        Assert.Equal(1, snapshot.Passed);
        Assert.Equal(0, snapshot.Failed);
        Assert.Equal(1, snapshot.Pending);
        Assert.Equal(1, snapshot.Translated);
    }

    [Fact]
    public void Summarize_ProtocolFailedUnitWithNoText_IsNeitherTranslatedNorPending()
    {
        // 批次协议失败（响应缺该编号）的行：状态被判 LQA_FAILED、译文为空。
        // 该行既没有译文（不计入进度），也不在待译集合内（EXTRACTED 才是），
        // 由 UI 的失败计数与单行 Re-try 承接 —— 这是 VM 侧「只对已获译文的行送
        // run_lqa」这条过滤规则的依据，在此固化为契约。
        var snapshot = TranslationProgress.Summarize([("LQA_FAILED", null)]);

        Assert.Equal(1, snapshot.Total);
        Assert.Equal(0, snapshot.Translated);
        Assert.Equal(0, snapshot.Pending);
        Assert.Equal(1, snapshot.Failed);
    }

    [Fact]
    public void PendingDecaysMonotonicallyAsTranslationsLand()
    {
        // 衰减性：每落地一条译文，待译计数严格减一，已译计数严格加一
        var statuses = new List<(string?, string?)>();
        for (var i = 0; i < 10; i++)
        {
            statuses.Add(("EXTRACTED", null));
        }

        var previous = TranslationProgress.Summarize(statuses);
        Assert.Equal(10, previous.Pending);

        for (var i = 0; i < statuses.Count; i++)
        {
            statuses[i] = ("LQA_PASSED", $"译文{i}");
            var current = TranslationProgress.Summarize(statuses);

            Assert.Equal(previous.Pending - 1, current.Pending);
            Assert.Equal(previous.Translated + 1, current.Translated);
            Assert.True(current.Ratio >= previous.Ratio, "译文覆盖率不得回退");
            previous = current;
        }

        Assert.Equal(0, previous.Pending);
    }

    [Fact]
    public void Ratio_AlwaysStaysWithinUnitInterval()
    {
        for (var total = 0; total <= 12; total++)
        {
            for (var translated = 0; translated <= total; translated++)
            {
                var rows = new List<(string?, string?)>();
                rows.AddRange(Enumerable.Repeat<(string?, string?)>(("LQA_PASSED", "x"), translated));
                rows.AddRange(Enumerable.Repeat<(string?, string?)>(("EXTRACTED", null), total - translated));

                var snapshot = TranslationProgress.Summarize(rows);

                Assert.InRange(snapshot.Ratio, 0.0, 1.0);
                Assert.InRange(snapshot.Percent, 0, 100);
            }
        }
    }

    // --------------------------- 文案稳定性 ---------------------------

    [Fact]
    public void Text_MatchesDesignWording_WithTwoThirdsRoundingUp()
    {
        var rows = new List<(string?, string?)>
        {
            ("LQA_PASSED", "a"),
            ("LQA_PASSED", "b"),
            ("EXTRACTED", null),
        };
        // 2/3 = 66.67% → 设计稿口径的 67%
        Assert.Equal("67% Translated", TranslationProgress.Summarize(rows).Text);
    }

    [Theory]
    [InlineData("fr-FR")]
    [InlineData("de-DE")]
    [InlineData("zh-CN")]
    [InlineData("en-US")]
    public void Text_DoesNotDriftWithCulture(string culture)
    {
        // ToString("P0") 会在 fr-FR 等区域性下把百分号前加空格，令文案与断言都不可复现。
        // 这里强制切换区域性，锁死手工取整的实现不被「优化」回格式化字符串。
        var original = CultureInfo.CurrentCulture;
        try
        {
            CultureInfo.CurrentCulture = new CultureInfo(culture);
            var rows = new List<(string?, string?)>
            {
                ("LQA_PASSED", "a"),
                ("LQA_PASSED", "b"),
                ("EXTRACTED", null),
            };
            Assert.Equal("67% Translated", TranslationProgress.Summarize(rows).Text);
        }
        finally
        {
            CultureInfo.CurrentCulture = original;
        }
    }

    [Fact]
    public void Summarize_NullRows_Throws()
        => Assert.Throws<ArgumentNullException>(() => TranslationProgress.Summarize(null!));
}
