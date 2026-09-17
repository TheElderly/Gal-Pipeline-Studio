using GalPipeline.Core.Lqa;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// LQA Lab 看板聚合纯函数的契约测试 —— 违例聚合、规则分类与确定性排序。
/// </summary>
public class LqaBoardTests
{
    private static LqaViolationSource Source(
        string id, string rule, string severity, string message,
        string translated = "「訳」", string lineTag = "#00001") =>
        new(id, lineTag, "アリス", "「原文」", translated,
            [new LqaIssueLine(rule, severity, message)]);

    [Fact]
    public void Units_without_issues_are_excluded_from_the_board()
    {
        var clean = new LqaViolationSource("u-1", "#00001", "A", "原文", "「訳」", []);
        var broken = Source("u-2", "cjk_punctuation", "error", "「未闭合");

        var snapshot = LqaBoard.Build([clean, broken]);

        var item = Assert.Single(snapshot.Items);
        Assert.Equal("u-2", item.UnitId);
        Assert.Equal(1, snapshot.ViolationUnits);
        Assert.Equal(1, snapshot.ErrorIssues);
        Assert.Equal(0, snapshot.WarningIssues);
    }

    [Fact]
    public void Multiple_issues_on_one_unit_merge_into_a_single_row()
    {
        var source = new LqaViolationSource(
            "u-1", "#00007", "ミナ", "「原文」", "「訳」",
            [
                new LqaIssueLine("cjk_punctuation", "error", "「未闭合"),
                new LqaIssueLine("cjk_punctuation", "warning", "省略号应规范化为 ……"),
                new LqaIssueLine("control_conservation", "error", "译文缺少登记宏 [r]"),
            ]);

        var snapshot = LqaBoard.Build([source]);

        // 治理动作以「行」为单位：同单元多条违例合并为一个条目
        var item = Assert.Single(snapshot.Items);
        Assert.Equal(3, item.Issues.Count);
        Assert.Equal("cjk_punctuation", item.PrimaryRuleId); // 首条违例的规则
        Assert.Contains("未闭合", item.Summary);
        Assert.Contains("[r]", item.Summary);
        // 聚合按条数（而非单元数）：3 条违例 = 2 error + 1 warning
        Assert.Equal(3, snapshot.ErrorIssues + snapshot.WarningIssues);
        Assert.Equal(2, snapshot.ErrorIssues);
        Assert.Equal(1, snapshot.WarningIssues);
    }

    [Fact]
    public void Rules_are_sorted_error_count_first_then_deterministically()
    {
        var snapshot = LqaBoard.Build(
        [
            Source("u-1", "zzz_unknown", "warning", "w1"),
            Source("u-2", "zzz_unknown", "warning", "w2"),
            Source("u-3", "cjk_punctuation", "error", "e1"),
            Source("u-4", "cjk_punctuation", "error", "e2"),
            Source("u-5", "cjk_punctuation", "error", "e3"),
        ]);

        Assert.Equal(
            ["cjk_punctuation", "zzz_unknown"],
            snapshot.Rules.Select(r => r.RuleId).ToArray());
        var cjk = snapshot.Rules[0];
        Assert.Equal(3, cjk.Total);
        Assert.Equal(3, cjk.Errors);
        Assert.Equal(0, cjk.Warnings);
    }

    [Fact]
    public void Unknown_rule_ids_are_echoed_not_crashed()
    {
        var snapshot = LqaBoard.Build([Source("u-1", "brand_new_rule", "error", "新规则")]);

        Assert.Equal("brand_new_rule", Assert.Single(snapshot.Rules).RuleId);
        Assert.Equal("brand_new_rule", Assert.Single(snapshot.Items).PrimaryRuleId);
    }

    [Fact]
    public void Empty_input_yields_empty_snapshot()
    {
        var snapshot = LqaBoard.Build([]);

        Assert.Empty(snapshot.Items);
        Assert.Empty(snapshot.Rules);
        Assert.Equal(0, snapshot.ViolationUnits);
        Assert.Same(LqaBoardSnapshot.Empty, LqaBoard.Build([]));
    }

    [Fact]
    public void Output_order_follows_input_document_order()
    {
        var snapshot = LqaBoard.Build(
        [
            Source("u-2", "cjk_punctuation", "error", "b", lineTag: "#00002"),
            Source("u-1", "cjk_punctuation", "error", "a", lineTag: "#00001"),
        ]);

        // 顺序 = 输入行序（文档物理序），行号天然升序 —— 看板不许重排行
        Assert.Equal(["#00002", "#00001"], snapshot.Items.Select(i => i.LineTag).ToArray());
    }
}
