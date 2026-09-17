namespace GalPipeline.Core.Project;

/// <summary>资产条目：相对路径 + 字节量 + 分类（kind = scripts/images/audio）。</summary>
public sealed record AssetEntry(
    string RelativePath,
    string FullPath,
    long SizeBytes,
    string Kind,
    string Extension);

/// <summary>资产看板快照：三分类条目列表 + 计数（不可变，整存整取）。</summary>
public sealed record AssetBoardSnapshot(
    IReadOnlyList<AssetEntry> Scripts,
    IReadOnlyList<AssetEntry> Images,
    IReadOnlyList<AssetEntry> Audio,
    int TotalScanned,
    bool Truncated)
{
    public static AssetBoardSnapshot Empty { get; } = new([], [], [], 0, false);
}

/// <summary>
/// 标准工作区资产扫描纯函数 —— 扫描 ``.galpipeline/raw/`` 的三分类资产树。
///
/// 下沉共享库的理由与 LqaBoard 一致：聚合与分类会被算错的东西必须在
/// dotnet test 射程内。目录约定与 core/archive/workspace.py 严格对齐
/// （scripts / images / voice+bgm → audio）。
/// </summary>
public static class AssetWorkspace
{
    /// <summary>单分类扫描上限：防止超大解包产物拖垮看板（截断时如实标记）。</summary>
    public const int MaxEntriesPerKind = 2000;

    private static readonly HashSet<string> ScriptExtensions = new(StringComparer.OrdinalIgnoreCase)
        { ".ks", ".ks.txt", ".txt" };

    private static readonly HashSet<string> ImageExtensions = new(StringComparer.OrdinalIgnoreCase)
        { ".png", ".jpg", ".jpeg", ".bmp", ".tlg" };

    private static readonly HashSet<string> AudioExtensions = new(StringComparer.OrdinalIgnoreCase)
        { ".ogg", ".wav", ".mp3", ".flac" };

    /// <summary>按扩展名分类；未知扩展返回 null（调用方跳过）。</summary>
    public static string? Classify(string extension) =>
        ScriptExtensions.Contains(extension) ? "scripts"
        : ImageExtensions.Contains(extension) ? "images"
        : AudioExtensions.Contains(extension) ? "audio"
        : null;

    /// <summary>
    /// 扫描工作区 raw 目录。目录不存在 / 为空时返回 Empty（调用方据此
    /// 展示「尚未解包」引导态）；每分类超过 <see cref="MaxEntriesPerKind"/>
    /// 时截断并置 Truncated。
    /// </summary>
    public static AssetBoardSnapshot Scan(string? rawRoot)
    {
        if (string.IsNullOrWhiteSpace(rawRoot) || !Directory.Exists(rawRoot))
        {
            return AssetBoardSnapshot.Empty;
        }

        var scripts = new List<AssetEntry>();
        var images = new List<AssetEntry>();
        var audio = new List<AssetEntry>();
        var total = 0;
        var truncated = false;

        foreach (var kindDir in Directory.EnumerateDirectories(rawRoot))
        {
            var kindName = Path.GetFileName(kindDir).ToLowerInvariant();
            // 语音区兼容 voice/ 与 bgm/ 两个约定目录，统一归入 audio
            var kind = kindName switch
            {
                "scripts" => "scripts",
                "images" => "images",
                "voice" or "bgm" => "audio",
                _ => null,
            };
            if (kind is null)
            {
                continue;
            }

            IEnumerable<string> files;
            try
            {
                files = Directory.EnumerateFiles(kindDir, "*", SearchOption.AllDirectories);
            }
            catch (UnauthorizedAccessException)
            {
                continue; // 无权限子目录：跳过而非整板失败
            }

            foreach (var file in files)
            {
                var entry = MakeEntry(file, rawRoot, kind);
                if (entry is null)
                {
                    continue;
                }
                total++;
                var bucket = entry.Kind == "scripts" ? scripts
                    : entry.Kind == "images" ? images
                    : audio;
                if (bucket.Count >= MaxEntriesPerKind)
                {
                    truncated = true;
                    continue;
                }
                bucket.Add(entry);
            }
        }

        return new AssetBoardSnapshot(
            scripts.OrderBy(e => e.RelativePath, StringComparer.OrdinalIgnoreCase).ToList(),
            images.OrderBy(e => e.RelativePath, StringComparer.OrdinalIgnoreCase).ToList(),
            audio.OrderBy(e => e.RelativePath, StringComparer.OrdinalIgnoreCase).ToList(),
            total, truncated);
    }

    private static AssetEntry? MakeEntry(string fullPath, string rawRoot, string kind)
    {
        try
        {
            var info = new FileInfo(fullPath);
            var extension = info.Extension.ToLowerInvariant();
            // 目录与扩展名双重校验：voice/ 里的 txt 不算脚本，
            // scripts/ 里的 bin 也不算脚本（分类目录约定不是免检通行证）
            if (Classify(extension) != kind)
            {
                return null;
            }
            return new AssetEntry(
                Path.GetRelativePath(rawRoot, fullPath),
                fullPath,
                info.Length,
                kind,
                extension);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return null; // 文件瞬态占用 / 无权限：跳过单个文件，不拖垮扫描
        }
    }

    /// <summary>TLG 预览缓存的确定性落盘路径：指纹（路径|长度|mtime）SHA-256。</summary>
    public static string TlgCachePathFor(string sourcePath, string cacheDirectory)
    {
        var full = Path.GetFullPath(sourcePath);
        var info = new FileInfo(full);
        var fingerprint =
            $"{full.ToLowerInvariant()}|{info.Length}|{info.LastWriteTimeUtc.Ticks}";
        var hash = Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(
                System.Text.Encoding.UTF8.GetBytes(fingerprint))).ToLowerInvariant();
        return Path.Combine(cacheDirectory, hash + ".png");
    }
}
