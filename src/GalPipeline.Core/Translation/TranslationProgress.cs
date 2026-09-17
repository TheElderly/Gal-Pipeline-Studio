namespace GalPipeline.Core.Translation;

/// <summary>
/// 翻译进度统计的纯函数层：零依赖、无状态，供壳层视图模型与自动化测试共用。
///
/// 单独抽出来的理由：这一组判定与公式是全流程里最容易「看起来对、算起来错」的一环
/// —— 终态行不得计入待译、空白译文不得计入已译、空表不得除零、进度文案不得随区域性
/// 漂移。而壳层 <c>StudioViewModel</c> 属于 <c>net10.0-windows</c>，测试工程是
/// <c>net10.0</c>（无 UseWPF）**无法引用它**。把公式落到共享库里，才能在不启动 GUI
/// 的前提下把这些边界逐条钉死。
/// </summary>
public static class TranslationProgress
{
    /// <summary>已抽取待翻译的状态字面量（与 <c>core/models/status.py</c> 的 EXTRACTED 对齐）。</summary>
    public const string Extracted = "EXTRACTED";

    /// <summary>质检通过状态字面量。</summary>
    public const string Passed = "LQA_PASSED";

    /// <summary>质检失败状态字面量。</summary>
    public const string Failed = "LQA_FAILED";

    /// <summary>已导出终态字面量（闭环终点，见 <c>TranslationStatus.EXPORTED</c>）。</summary>
    public const string Exported = "EXPORTED";

    /// <summary>
    /// 待翻译判定：**已抽取且尚未产出任何译文**。
    ///
    /// 刻意只认 EXTRACTED 两件事都有理由：
    ///
    /// * <c>LQA_FAILED</c> 携带的是「已翻译但质检不合格」这个**有效结论**，
    ///   重翻该行应由单行 Re-try 承担；若把它计入待译，批量按钮会静默覆盖
    ///   用户已看到的失败诊断（引号未配平之类）。
    /// * <c>EXPORTED</c> / <c>LQA_PASSED</c> 已是终态，批量翻它们等于白烧额度。
    ///
    /// 译文空白用 <see cref="string.IsNullOrWhiteSpace"/>：只敲了几个空格不算已翻译。
    /// </summary>
    public static bool NeedsTranslation(string? status, string? translatedText)
        => string.Equals(status, Extracted, StringComparison.Ordinal)
           && string.IsNullOrWhiteSpace(translatedText);

    /// <summary>
    /// 已产出译文的判定（**不看质检结论**：失败行毕竟也有过一次译文尝试，
    /// 故计入「已翻译」进度；失败与否由 Passed/Failed 两个独立计数表达）。
    /// </summary>
    public static bool HasTranslation(string? translatedText)
        => !string.IsNullOrWhiteSpace(translatedText);

    /// <summary>按行快照聚合出全部统计量（单次遍历，O(n)）。</summary>
    public static TranslationProgressSnapshot Summarize(
        IEnumerable<(string? Status, string? TranslatedText)> rows)
    {
        ArgumentNullException.ThrowIfNull(rows);

        var total = 0;
        var translated = 0;
        var pending = 0;
        var passed = 0;
        var failed = 0;
        foreach (var (status, text) in rows)
        {
            total++;
            if (HasTranslation(text))
            {
                translated++;
            }
            if (NeedsTranslation(status, text))
            {
                pending++;
            }
            if (string.Equals(status, Passed, StringComparison.Ordinal)
                || string.Equals(status, Exported, StringComparison.Ordinal))
            {
                passed++;
            }
            else if (string.Equals(status, Failed, StringComparison.Ordinal))
            {
                failed++;
            }
        }
        return new TranslationProgressSnapshot(total, translated, pending, passed, failed);
    }
}

/// <summary>
/// 一次进度快照。不可变值类型 —— 统计是纯计算的产物，不应被就地改写。
/// </summary>
public readonly record struct TranslationProgressSnapshot(
    int Total,
    int Translated,
    int Pending,
    int Passed,
    int Failed)
{
    /// <summary>
    /// 译文覆盖率 ∈ [0, 1]：已有译文的行数 / 总行数。
    /// 总行数为 0 时收敛为 0 —— 不做除零保护的话，空表首屏会得到 NaN 并让进度条渲染异常。
    /// </summary>
    public double Ratio => Total == 0 ? 0.0 : (double)Translated / Total;

    /// <summary>设计稿格式的进度文案，如 <c>67% Translated</c>。</summary>
    public string Text => $"{Percent}% Translated";

    /// <summary>
    /// 百分比整数。手工取整而非 <c>ToString("P0")</c>：后者会随当前区域性
    /// 在数字与 % 之间插入空格（如 fr-FR），令文案与断言都不可复现。
    /// </summary>
    public int Percent => (int)Math.Round(Ratio * 100, MidpointRounding.AwayFromZero);
}
