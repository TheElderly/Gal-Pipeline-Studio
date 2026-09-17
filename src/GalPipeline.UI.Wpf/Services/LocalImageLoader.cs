using System.Collections.Concurrent;
using System.IO;
using System.Windows.Media.Imaging;

namespace GalPipeline.Desktop.Services;

/// <summary>
/// 本地图片解码服务（视觉修图工作台的解码底座）。
///
/// 为什么必须自带缓存键：WPF 的 <see cref="BitmapImage"/> 默认按 URI 命中
/// 全局图片缓存 —— 外部修图软件**覆盖保存同名文件**后，重新构造 BitmapImage
/// 会拿到旧像素，"热重载"变成假象。因此这里以
/// （路径, 最后写入时间, 解码宽度）为缓存键，并在解码时显式
/// <see cref="BitmapCacheOption.OnLoad"/> + <see cref="BitmapCreateOptions.IgnoreImageCache"/>
/// 强制读盘，解码完成后 Freeze 以便跨线程共享。
///
/// 全流程本地离线：只读磁盘字节，不做任何网络访问。
/// </summary>
public sealed class LocalImageLoader
{
    /// <summary>解码结果缓存（键含 mtime，覆盖保存必然产生新键）。</summary>
    private readonly ConcurrentDictionary<string, BitmapSource> _cache = new();

    /// <summary>工作台支持解码的图片扩展名（WPF 原生解码器覆盖范围）。</summary>
    public static readonly IReadOnlyList<string> SupportedExtensions =
    [
        ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif",
    ];

    /// <summary>人类可读的扩展名清单（UI 提示文案的唯一真源）。</summary>
    public static string SupportedExtensionLabel =>
        string.Join(" / ", SupportedExtensions);

    /// <summary>文件是否属于工作台可解码范围（按扩展名判定，不读内容）。</summary>
    public static bool IsSupported(string path) =>
        SupportedExtensions.Contains(Path.GetExtension(path), StringComparer.OrdinalIgnoreCase);

    /// <summary>
    /// 同步解码一张图片；<paramref name="decodePixelWidth"/> &gt; 0 时按该宽度缩放解码
    /// （缩略图走这条路径，避免为列表把 4K 全量位图读进内存）。
    /// 解码失败（损坏文件/非图片/句柄占用）返回 null —— 由调用方决定呈现策略。
    /// </summary>
    public BitmapSource? Load(string path, int decodePixelWidth = 0)
    {
        try
        {
            var stamp = File.GetLastWriteTimeUtc(path).Ticks;
            var length = new FileInfo(path).Length;
            var key = $"{path}|{stamp}|{length}|{decodePixelWidth}";
            if (_cache.TryGetValue(key, out var cached))
            {
                return cached;
            }

            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;      // 立即读盘，释放文件句柄
            bitmap.CreateOptions = BitmapCreateOptions.IgnoreImageCache;  // 覆盖保存必须重新解码
            bitmap.UriSource = new Uri(Path.GetFullPath(path));
            if (decodePixelWidth > 0)
            {
                bitmap.DecodePixelWidth = decodePixelWidth;
            }
            bitmap.EndInit();
            bitmap.Freeze();   // 冻结后可跨线程持有（后台解码 → UI 线程直接绑定）

            _cache[key] = bitmap;
            TrimCache();
            return bitmap;
        }
        catch (Exception ex) when (ex is IOException
                                      or UnauthorizedAccessException
                                      or NotSupportedException
                                      or ArgumentException
                                      or FileFormatException)
        {
            return null;
        }
    }

    /// <summary>解码像素尺寸（未解码时返回 null）——列表的「分辨率」标签用。</summary>
    public static string DescribePixelSize(BitmapSource? image) =>
        image is null ? "—" : $"{image.PixelWidth} × {image.PixelHeight}";

    /// <summary>丢弃某路径的全部缓存条目（文件被删除或重命名时调用）。</summary>
    public void Invalidate(string path)
    {
        foreach (var key in _cache.Keys.Where(k => k.StartsWith(path + "|", StringComparison.OrdinalIgnoreCase)))
        {
            _cache.TryRemove(key, out _);
        }
    }

    /// <summary>缓存上限保护：超过 256 条时按插入顺序淘汰（简单 FIFO，够用且无锁竞争）。</summary>
    private void TrimCache()
    {
        const int MaxEntries = 256;
        if (_cache.Count <= MaxEntries)
        {
            return;
        }
        foreach (var key in _cache.Keys.Take(_cache.Count - MaxEntries))
        {
            _cache.TryRemove(key, out _);
        }
    }
}
