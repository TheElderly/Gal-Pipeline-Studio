using GalPipeline.Core.Project;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 资产工作区扫描纯函数的契约测试 —— 分类、排序、截断与缓存指纹。
/// </summary>
public class AssetWorkspaceTests : IDisposable
{
    private readonly string _root;

    public AssetWorkspaceTests()
    {
        _root = Path.Combine(Path.GetTempPath(), "galpipeline-tests", Guid.NewGuid().ToString("N"));
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
            // 临时目录被句柄占用时交给系统清理，不影响断言
        }
    }

    private string WriteAsset(string relativePath, string content = "x")
    {
        var path = Path.Combine(_root, relativePath);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, content);
        return path;
    }

    [Fact]
    public void Scans_three_kinds_with_relative_paths()
    {
        WriteAsset(Path.Combine("raw", "scripts", "scene01.ks"));
        WriteAsset(Path.Combine("raw", "images", "chara.tlg"));
        WriteAsset(Path.Combine("raw", "images", "bg01.png"));
        WriteAsset(Path.Combine("raw", "voice", "vo_00012.ogg"));
        WriteAsset(Path.Combine("raw", "bgm", "title.ogg"));

        var snapshot = AssetWorkspace.Scan(Path.Combine(_root, "raw"));

        Assert.Single(snapshot.Scripts);
        Assert.Equal(2, snapshot.Images.Count);
        Assert.Equal(2, snapshot.Audio.Count); // voice/ 与 bgm/ 同归 audio
        Assert.Equal(5, snapshot.TotalScanned);
        Assert.False(snapshot.Truncated);
        Assert.Contains(snapshot.Scripts, e => e.RelativePath.EndsWith("scene01.ks"));
    }

    [Fact]
    public void Missing_root_returns_empty_snapshot()
    {
        var snapshot = AssetWorkspace.Scan(Path.Combine(_root, "no-such-raw"));

        Assert.Empty(snapshot.Scripts);
        Assert.Empty(snapshot.Images);
        Assert.Empty(snapshot.Audio);
        Assert.Same(AssetBoardSnapshot.Empty, AssetWorkspace.Scan(null));
    }

    [Fact]
    public void Unknown_directories_and_extensions_are_skipped()
    {
        WriteAsset(Path.Combine("raw", "misc", "note.txt")); // 未登记目录 → 跳过
        WriteAsset(Path.Combine("raw", "images", "data.bin")); // 登记目录里的未知扩展 → 跳过

        var snapshot = AssetWorkspace.Scan(Path.Combine(_root, "raw"));

        Assert.Empty(snapshot.Scripts);
        Assert.Empty(snapshot.Images);
        Assert.Empty(snapshot.Audio);
    }

    [Fact]
    public void Classification_by_extension_wins_over_directory_for_audio_dirs()
    {
        // voice/ 里的 txt 不算脚本（分类目录 + 扩展名双重约束）
        WriteAsset(Path.Combine("raw", "voice", "readme.txt"));

        var snapshot = AssetWorkspace.Scan(Path.Combine(_root, "raw"));

        Assert.Empty(snapshot.Scripts);
        Assert.Empty(snapshot.Audio);
    }

    [Fact]
    public void Truncates_at_max_entries_per_kind()
    {
        var scripts = Path.Combine(_root, "raw", "scripts");
        Directory.CreateDirectory(scripts);
        for (var i = 0; i < AssetWorkspace.MaxEntriesPerKind + 50; i++)
        {
            File.WriteAllText(Path.Combine(scripts, $"scene{i:00000}.ks"), "x");
        }

        var snapshot = AssetWorkspace.Scan(Path.Combine(_root, "raw"));

        Assert.Equal(AssetWorkspace.MaxEntriesPerKind, snapshot.Scripts.Count);
        Assert.True(snapshot.Truncated);
    }

    [Fact]
    public void Tlg_cache_path_is_deterministic_and_content_sensitive()
    {
        var a = WriteAsset(Path.Combine("game", "a.tlg"), "v1");
        var cache = Path.Combine(_root, "cache");

        var first = AssetWorkspace.TlgCachePathFor(a, cache);
        var again = AssetWorkspace.TlgCachePathFor(a, cache);

        Assert.Equal(first, again); // 同文件恒同键（缓存命中）
        Assert.EndsWith(".png", first);
        Assert.Contains(cache, first);

        File.WriteAllText(a, "v2"); // 内容变化 → 指纹变化
        Assert.NotEqual(first, AssetWorkspace.TlgCachePathFor(a, cache));
    }
}
