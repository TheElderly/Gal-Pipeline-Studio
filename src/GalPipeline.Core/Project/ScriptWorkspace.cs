namespace GalPipeline.Core.Project;

/// <summary>工程内一个剧本文件的扫描条目（相对路径用于展示与去重）。</summary>
public sealed record ScriptFileEntry(string FullPath, string RelativePath, long SizeBytes);

/// <summary>
/// 工程剧本扫描与切换防丢失决策 —— 模块 1「工程树」的数据底座。
///
/// 纯函数、零 WPF 依赖：壳层 VM（net10.0-windows）与自动化测试
/// （net10.0）消费同一份实现，扫描排序与「是否有未保存译文」的判定
/// 才能被无 GUI 测试钉住，而不是靠肉眼点树验证。
/// </summary>
public static class ScriptWorkspace
{
    /// <summary>各引擎的剧本文件搜索模式（小写；新增引擎 = 追加一条）。</summary>
    private static readonly IReadOnlyDictionary<string, string[]> ScriptPatterns =
        new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
        {
            // KiriKiri/KAG：剧本即 .ks 文本（.ks.txt 变体也常见）
            ["kirikiri"] = ["*.ks", "*.ks.txt"],
            // BGI / CatSystem2 / Majiro：脚本在封包内，须先解包再抽取 —— 暂无散文件契约
            ["bgi"] = [],
            ["catsystem2"] = [],
            ["majiro"] = [],
        };

    /// <summary>单次扫描的文件数上限（整作可能上万文件；树只取前 N，超出提示用户细化目录）。</summary>
    public const int MaxScripts = 500;

    /// <summary>
    /// 扫描游戏根目录下的剧本文件（递归），按**自然顺序**（路径段内数字按数值比较）
    /// 排序 —— ``scenario02.ks`` 必须排在 ``scenario10.ks`` 之前，字典序会颠倒它们。
    /// 未注册扫描模式的引擎返回空列表（解包后再扫描，属后续切片）。
    /// </summary>
    public static IReadOnlyList<ScriptFileEntry> ScanScripts(string gameDir, string? engineType)
    {
        if (string.IsNullOrWhiteSpace(gameDir) || !Directory.Exists(gameDir))
        {
            return [];
        }
        if (engineType is null
            || !ScriptPatterns.TryGetValue(engineType, out var patterns)
            || patterns.Length == 0)
        {
            return [];
        }

        var root = Path.GetFullPath(gameDir);
        var entries = new List<ScriptFileEntry>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var pattern in patterns)
        {
            foreach (var file in Directory.EnumerateFiles(root, pattern, SearchOption.AllDirectories))
            {
                var full = Path.GetFullPath(file);
                if (!seen.Add(full))
                {
                    continue; // *.ks 与 *.ks.txt 通配可能重叠同一文件
                }
                var info = new FileInfo(full);
                entries.Add(new ScriptFileEntry(
                    FullPath: full,
                    RelativePath: Path.GetRelativePath(root, full),
                    SizeBytes: info.Length));
                if (entries.Count >= MaxScripts)
                {
                    break;
                }
            }
            if (entries.Count >= MaxScripts)
            {
                break;
            }
        }

        entries.Sort(CompareByRelativePath);
        return entries;
    }

    /// <summary>自然顺序比较：数字段按数值、非数字段按忽略大小写字典序。</summary>
    private static int CompareByRelativePath(ScriptFileEntry left, ScriptFileEntry right) =>
        CompareNatural(left.RelativePath, right.RelativePath, StringComparison.OrdinalIgnoreCase);

    public static int CompareNatural(
        string left,
        string right,
        StringComparison comparison = StringComparison.OrdinalIgnoreCase)
    {
        var li = 0;
        var ri = 0;
        while (li < left.Length && ri < right.Length)
        {
            if (char.IsDigit(left[li]) && char.IsDigit(right[ri]))
            {
                var leftEnd = ScanDigits(left, li);
                var rightEnd = ScanDigits(right, ri);
                var leftNumber = long.Parse(left[li..leftEnd], System.Globalization.CultureInfo.InvariantCulture);
                var rightNumber = long.Parse(right[ri..rightEnd], System.Globalization.CultureInfo.InvariantCulture);
                if (leftNumber != rightNumber)
                {
                    return leftNumber.CompareTo(rightNumber);
                }
                // 数值相等时较短前导零串排前（scenario01 < scenario001）
                var lengthOrder = (leftEnd - li).CompareTo(rightEnd - ri);
                if (lengthOrder != 0)
                {
                    return lengthOrder;
                }
                li = leftEnd;
                ri = rightEnd;
                continue;
            }
            var charCompare = string.Compare(
                left, li, right, ri, 1, comparison);
            if (charCompare != 0)
            {
                return charCompare;
            }
            li++;
            ri++;
        }
        return (left.Length - li).CompareTo(right.Length - ri);
    }

    private static int ScanDigits(string text, int start)
    {
        var index = start;
        while (index < text.Length && char.IsDigit(text[index]))
        {
            index++;
        }
        return index;
    }

    /// <summary>
    /// 解包引导判定（纯函数）：引擎已识别**且**未扫出任何裸剧本 ——
    /// 典型形态是「data.xp3 / *.arc 纯封包盘」，此时工程树应给出
    /// 「一键解包」引导而不是留一块空面板。
    /// </summary>
    public static bool NeedsUnpack(bool engineDetected, int scriptCount) =>
        engineDetected && scriptCount == 0;

    /// <summary>
    /// 切换剧本前的**防丢失判定**：只要存在任何已填写译文的行，就先自动落盘。
    ///
    /// 口径刻意宽松（不看质检结论、不看是否已导出）：审校半成品也是劳动
    /// 成果 —— 覆盖写 localized/ 幂等且不碰原文件，宁可多存一次，
    /// 也绝不静默丢掉用户敲的字。
    /// </summary>
    public static bool HasUnsavedTranslations(
        IEnumerable<(string? Status, string? TranslatedText)> units) =>
        units.Any(unit => !string.IsNullOrWhiteSpace(unit.TranslatedText));
}
