using GalPipeline.Core.Project;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 工程剧本扫描与切换防丢失决策的无头测试（P1 工程树的数据底座）。
/// 全部用 tmp_path 合成虚拟游戏目录，真实走文件系统。
/// </summary>
public class ScriptWorkspaceTests : IDisposable
{
    private readonly string _root;

    public ScriptWorkspaceTests()
    {
        _root = Path.Combine(Path.GetTempPath(), "galpipeline-project", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_root);
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_root, recursive: true);
        }
        catch
        {
            // 句柄竞争交给系统临时目录清理
        }
    }

    private string Write(params string[] relativeParts)
    {
        var path = Path.Combine([_root, .. relativeParts]);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllBytes(path, new byte[16]);
        return path;
    }

    // ------------------------------------------------------------------
    // 1. 扫描：递归 + 大小写不敏感 + 去重
    // ------------------------------------------------------------------

    [Fact]
    public void Scan_lists_ks_scripts_recursively_for_kirikiri()
    {
        Write("scenario", "scene01.ks");
        Write("scenario", "sub", "scene02.ks");
        Write("root.ks");
        Write("assets", "bg.png"); // 非剧本，不计入

        var entries = ScriptWorkspace.ScanScripts(_root, "kirikiri");

        Assert.Equal(3, entries.Count);
        Assert.All(entries, e => Assert.True(File.Exists(e.FullPath)));
        Assert.All(entries, e => Assert.StartsWith(_root, e.FullPath));
        Assert.All(entries, e => Assert.EndsWith(".ks", e.RelativePath));
        Assert.Equal(16, entries[0].SizeBytes);
    }

    [Fact]
    public void Scan_is_case_insensitive_and_deduplicates_overlapping_patterns()
    {
        Write("A.KS");       // 大写扩展名
        Write("b.ks.txt");   // ks.txt 变体
        // 两个模式（*.ks 与 *.ks.txt）不会互相重复，但同一路径只出现一次
        var entries = ScriptWorkspace.ScanScripts(_root, "kirikiri");
        Assert.Equal(2, entries.Count);
        Assert.Equal(
            entries.Select(e => e.FullPath).Distinct(StringComparer.OrdinalIgnoreCase).Count(),
            entries.Count);
    }

    // ------------------------------------------------------------------
    // 2. 排序：自然顺序（数字按数值，scenario02 < scenario10）
    // ------------------------------------------------------------------

    [Fact]
    public void Scan_orders_numerically_not_lexicographically()
    {
        foreach (var name in new[] { "scenario10", "scenario2", "scenario1", "scenario100" })
        {
            Write("scenario", $"{name}.ks");
        }

        var entries = ScriptWorkspace.ScanScripts(_root, "kirikiri");

        Assert.Equal(
        [
            "scenario1.ks",
            "scenario2.ks",
            "scenario10.ks",
            "scenario100.ks",
        ], entries.Select(e => Path.GetFileName(e.RelativePath)).ToArray());
    }

    [Fact]
    public void Natural_comparison_handles_leading_zeroes_stably()
    {
        Assert.True(
            ScriptWorkspace.CompareNatural("scenario01.ks", "scenario001.ks") < 0,
            "数值相等时较短前导零串排前，保证排序全序稳定");
        Assert.Equal(0, ScriptWorkspace.CompareNatural("a2b.ks", "a2b.ks"));
    }

    // ------------------------------------------------------------------
    // 3. 边界：未知引擎 / 目录不存在 / 上限
    // ------------------------------------------------------------------

    [Fact]
    public void Unknown_engine_scans_nothing_until_its_contract_exists()
    {
        Write("data.arc");
        Assert.Empty(ScriptWorkspace.ScanScripts(_root, "bgi"));
        Assert.Empty(ScriptWorkspace.ScanScripts(_root, null));
        Assert.Empty(ScriptWorkspace.ScanScripts(_root, "unknown-engine"));
    }

    [Fact]
    public void Missing_directory_scans_nothing_without_throwing()
    {
        Assert.Empty(ScriptWorkspace.ScanScripts(Path.Combine(_root, "nope"), "kirikiri"));
        Assert.Empty(ScriptWorkspace.ScanScripts("", "kirikiri"));
    }

    [Fact]
    public void Scan_respects_max_scripts_guard()
    {
        for (var i = 0; i < ScriptWorkspace.MaxScripts + 10; i++)
        {
            Write($"batch{i:0000}.ks");
        }

        var entries = ScriptWorkspace.ScanScripts(_root, "kirikiri");

        Assert.Equal(ScriptWorkspace.MaxScripts, entries.Count);
    }

    // ------------------------------------------------------------------
    // 5. 解包引导判定：识别到引擎但无裸剧本 → 一键解包提示
    // ------------------------------------------------------------------

    [Fact]
    public void Detected_engine_with_no_scripts_needs_unpack_prompt()
    {
        Assert.True(ScriptWorkspace.NeedsUnpack(engineDetected: true, scriptCount: 0));
        Assert.False(ScriptWorkspace.NeedsUnpack(engineDetected: true, scriptCount: 3));
        Assert.False(ScriptWorkspace.NeedsUnpack(engineDetected: false, scriptCount: 0),
            "引擎都识别不了时无从解包：提示无意义");
    }

    // ------------------------------------------------------------------
    // 6. 防丢失判定：有任何译文即自动落盘（不看质检结论）
    // ------------------------------------------------------------------

    [Fact]
    public void Any_translated_row_triggers_auto_save()
    {
        Assert.True(ScriptWorkspace.HasUnsavedTranslations(
        [
            ("LQA_PASSED", "「好」"),
            ("EXTRACTED", null),
        ]));
        // 半成品（未过质检）同样是劳动成果：宁多存不丢失
        Assert.True(ScriptWorkspace.HasUnsavedTranslations(
        [
            ("LQA_FAILED", "「缺半个引号"),
        ]));
    }

    [Fact]
    public void Whitespace_only_translations_do_not_count_as_saved_work()
    {
        Assert.False(ScriptWorkspace.HasUnsavedTranslations(
        [
            ("EXTRACTED", null),
            ("EXTRACTED", "   "),
            ("LQA_PASSED", ""),
        ]));
    }
}
