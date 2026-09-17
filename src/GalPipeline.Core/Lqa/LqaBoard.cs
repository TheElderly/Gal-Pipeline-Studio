namespace GalPipeline.Core.Lqa;

/// <summary>单条 LQA 违例明细（RuleID + 级别 + 描述，与 metadata["lqa_issues"] 逐项对应）。</summary>
public sealed record LqaIssueLine(string RuleId, string Severity, string Message);

/// <summary>违规看板的输入行：一个带违例留痕的单元（壳层负责从行 VM 提取）。</summary>
public sealed record LqaViolationSource(
    string UnitId,
    string LineTag,
    string Speaker,
    string SourceText,
    string TranslatedText,
    IReadOnlyList<LqaIssueLine> Issues);

/// <summary>按规则类型聚合的统计（rule_id → 条数，区分 error / warning）。</summary>
public sealed record LqaRuleSummary(string RuleId, int Total, int Errors, int Warnings);

/// <summary>看板中的一条违规：行级定位信息 + 违例明细 + 聚合摘要。</summary>
public sealed record LqaViolationItem(
    string UnitId,
    string LineTag,
    string Speaker,
    string SourceText,
    string TranslatedText,
    IReadOnlyList<LqaIssueLine> Issues,
    string PrimaryRuleId,
    string Summary);

/// <summary>质量门禁看板快照：违规列表 + 规则聚合 + 汇总计数（不可变，整存整取）。</summary>
public sealed record LqaBoardSnapshot(
    IReadOnlyList<LqaViolationItem> Items,
    IReadOnlyList<LqaRuleSummary> Rules,
    int ViolationUnits,
    int ErrorIssues,
    int WarningIssues)
{
    public static LqaBoardSnapshot Empty { get; } = new(
        [], [], 0, 0, 0);
}

/// <summary>
/// LQA Lab 看板的聚合纯函数 —— 从带违例留痕的单元行构建分类看板。
///
/// 下沉共享库的理由与统计层一致：壳层 VM 属 net10.0-windows，测试工程
/// 引用不了；「聚合与分类会被算错」的东西必须在 dotnet test 射程内。
/// 输入只依赖数据本身（行号/角色/原文/译文/违例明细），零 WPF 依赖。
/// </summary>
public static class LqaBoard
{
    /// <summary>规则 ID → 中文标签（看板聚合徽章的显示名；未知规则原样回显）。</summary>
    private static readonly Dictionary<string, string> RuleLabels = new()
    {
        ["cjk_punctuation"] = "成对标点不守恒",
        ["atomic_conservation"] = "宏锚点越界",
        ["paired_balance"] = "成对标记配平",
        ["control_conservation"] = "控制符守恒",
        ["translator_protocol"] = "批次协议失败",
    };

    public static string RuleLabel(string ruleId) =>
        RuleLabels.TryGetValue(ruleId, out var label) ? label : ruleId;

    /// <summary>
    /// 构建看板快照。三条纪律：
    /// 顺序 = 输入行序（文档物理序，行号天然升序）；同单元多条违例合并为一个
    /// 条目（治理动作以「行」为单位）；聚合按 rule_id 分组、error 优先呈现。
    /// </summary>
    public static LqaBoardSnapshot Build(IEnumerable<LqaViolationSource> sources)
    {
        // 先物化再判空（调用方常传惰性 LINQ 枚举，直接判 ICollection 会漏算）；
        // 空输入短路返回 Empty 单例（record 不可变，共享安全）：全绿路径
        // 恒得同一份快照，调用方无需特判
        var materialized = sources as IReadOnlyList<LqaViolationSource> ?? sources.ToList();
        if (materialized.Count == 0)
        {
            return LqaBoardSnapshot.Empty;
        }

        var items = new List<LqaViolationItem>();
        var ruleStats = new Dictionary<string, (int Total, int Errors, int Warnings)>();
        var errorIssues = 0;
        var warningIssues = 0;

        foreach (var source in materialized)
        {
            if (source.Issues.Count == 0)
            {
                continue; // 无违例留痕的行不进看板（绿行不该出现在质检中心）
            }

            var summary = string.Join("；", source.Issues.Select(i => i.Message));
            items.Add(new LqaViolationItem(
                source.UnitId, source.LineTag, source.Speaker,
                source.SourceText, source.TranslatedText,
                source.Issues, source.Issues[0].RuleId, summary));

            foreach (var issue in source.Issues)
            {
                var (total, errors, warnings) = ruleStats.GetValueOrDefault(
                    issue.RuleId, (0, 0, 0));
                ruleStats[issue.RuleId] = issue.Severity == "error"
                    ? (total + 1, errors + 1, warnings)
                    : (total + 1, errors, warnings + 1);
                if (issue.Severity == "error")
                {
                    errorIssues++;
                }
                else
                {
                    warningIssues++;
                }
            }
        }

        var rules = ruleStats
            .Select(kv => new LqaRuleSummary(kv.Key, kv.Value.Total, kv.Value.Errors, kv.Value.Warnings))
            .OrderByDescending(r => r.Errors)   // error 多的规则最需要治理，排最前
            .ThenByDescending(r => r.Total)
            .ThenBy(r => r.RuleId, StringComparer.Ordinal) // 平分时确定性收尾
            .ToList();

        return new LqaBoardSnapshot(items, rules, items.Count, errorIssues, warningIssues);
    }
}
