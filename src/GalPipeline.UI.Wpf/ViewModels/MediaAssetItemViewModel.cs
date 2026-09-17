using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>图片资产的修图生命周期状态（工作台的一等状态，驱动角标色与检索）。</summary>
public enum MediaAssetStatus
{
    /// <summary>未修改：磁盘原样，尚未开工。</summary>
    Untouched,

    /// <summary>修图中：已进入人工修图队列。</summary>
    Editing,

    /// <summary>已完成：本轮修图收工，等待打包。</summary>
    Completed,
}

/// <summary>
/// 资产墙中的一张图片卡片（视觉修图工作台）。
///
/// 只承载**数据与状态**：缩略图由 <see cref="MediaViewModel"/> 经解码服务回填，
/// 视图层只做绑定与模板化呈现。
/// </summary>
public sealed partial class MediaAssetItemViewModel : ObservableObject
{
    // 状态角标：暗底低饱和三态色（沿用 Studio 徽章的视觉强度纪律：
    // 饱和实心填充 + 高对比冷白文字，不做高光霓虹）。
    private static readonly Brush UntouchedBrush = Frozen(0x3A, 0x3A, 0x3C);
    private static readonly Brush EditingBrush = Frozen(0x2F, 0x5C, 0x9E);
    private static readonly Brush CompletedBrush = Frozen(0x2E, 0x9E, 0x4F);

    private static Brush Frozen(byte r, byte g, byte b)
    {
        var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
        brush.Freeze();
        return brush;
    }

    public MediaAssetItemViewModel(string fullPath)
    {
        FullPath = fullPath;
        FileName = Path.GetFileName(fullPath);
        DirectoryPath = Path.GetDirectoryName(fullPath) ?? string.Empty;
        Touch();
    }

    public string FullPath { get; }

    public string FileName { get; }

    public string DirectoryPath { get; }

    /// <summary>文件字节数（热重载时随磁盘刷新）。</summary>
    public long SizeBytes { get; private set; }

    /// <summary>最后写入时间（热重载时随磁盘刷新）。</summary>
    public DateTime LastWriteTime { get; private set; }

    /// <summary>修图状态（用户标记，或由外部保存事件驱动）。</summary>
    [ObservableProperty]
    public partial MediaAssetStatus Status { get; set; } = MediaAssetStatus.Untouched;

    /// <summary>列表缩略图（后台解码后回填；解码失败保持 null，模板呈现提示）。</summary>
    [ObservableProperty]
    public partial BitmapSource? Thumbnail { get; set; }

    /// <summary>缩略图解码失败标记（模板据此显示「无法解码」角标）。</summary>
    [ObservableProperty]
    public partial bool ThumbnailFailed { get; set; }

    /// <summary>状态文案（角标 + 检索都吃这一份）。</summary>
    public string StatusLabel => Status switch
    {
        MediaAssetStatus.Editing => "修图中",
        MediaAssetStatus.Completed => "已完成",
        _ => "未修改",
    };

    /// <summary>状态角标底色。</summary>
    public Brush StatusBrush => Status switch
    {
        MediaAssetStatus.Editing => EditingBrush,
        MediaAssetStatus.Completed => CompletedBrush,
        _ => UntouchedBrush,
    };

    /// <summary>角标文字色：高对比冷白（暗底可读性）。</summary>
    public Brush StatusForeground { get; } = Frozen(0xEE, 0xF8, 0xFF);

    /// <summary>卡片副标题：扩展名 + 体积 + 修改时间。</summary>
    public string MetaLabel =>
        $"{Path.GetExtension(FileName).TrimStart('.').ToUpperInvariant()} · {SizeLabel} · {LastWriteTime:MM-dd HH:mm}";

    /// <summary>人类可读体积。</summary>
    public string SizeLabel => SizeBytes switch
    {
        >= 1024 * 1024 => $"{SizeBytes / 1024.0 / 1024.0:0.0} MB",
        >= 1024 => $"{SizeBytes / 1024.0:0.0} KB",
        _ => $"{SizeBytes} B",
    };

    /// <summary>分辨率标签（缩略图解码后回填；未解码为「—」）。</summary>
    public string PixelSizeLabel =>
        Thumbnail is null ? "—" : $"{Thumbnail.PixelWidth} × {Thumbnail.PixelHeight}";

    /// <summary>从磁盘刷新体积/时间戳（FileSystemWatcher 与手动刷新共用）。</summary>
    public void Touch()
    {
        try
        {
            var info = new FileInfo(FullPath);
            SizeBytes = info.Exists ? info.Length : 0;
            LastWriteTime = info.Exists ? info.LastWriteTime : default;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            SizeBytes = 0;
            LastWriteTime = default;
        }
        OnPropertyChanged(nameof(SizeBytes));
        OnPropertyChanged(nameof(LastWriteTime));
        OnPropertyChanged(nameof(SizeLabel));
        OnPropertyChanged(nameof(MetaLabel));
    }

    /// <summary>回填缩略图并同步派生标签。</summary>
    public void ApplyThumbnail(BitmapSource? thumbnail)
    {
        Thumbnail = thumbnail;
        ThumbnailFailed = thumbnail is null;
        OnPropertyChanged(nameof(PixelSizeLabel));
    }

    partial void OnStatusChanged(MediaAssetStatus value)
    {
        OnPropertyChanged(nameof(StatusLabel));
        OnPropertyChanged(nameof(StatusBrush));
    }
}
