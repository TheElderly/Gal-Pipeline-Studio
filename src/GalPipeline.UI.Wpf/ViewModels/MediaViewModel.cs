using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Data;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Desktop.Services;
using Microsoft.Win32;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>资产墙的状态筛选（与检索框组合生效）。</summary>
public enum MediaStatusFilter
{
    All,
    Untouched,
    Editing,
    Completed,
}

/// <summary>原图 vs 修图的对比形态。</summary>
public enum MediaCompareMode
{
    /// <summary>单图视图（支持缩放与平移）。</summary>
    None,

    /// <summary>滑动帘：同一画布上按比例裁剪出「原图快照」层。</summary>
    Curtain,

    /// <summary>分屏：左右两半各自等比铺满。</summary>
    Split,
}

/// <summary>
/// Media 视觉修图工作台视图模型。
///
/// 职责边界（MVVM 纯洁度）：
///   · 全部命令与状态归位于此（视图层零业务判断）；
///   · 视图层只承担两类纯几何/宿主事务：滑动帘裁剪矩形与 Snackbar 呈现；
///   · 图片解码走 <see cref="LocalImageLoader"/>，本 VM 不碰 WPF 图片管线细节。
///
/// 热重载语义（可验证的诚实定义）：
///   外部修图软件**覆盖保存**同名文件 → FileSystemWatcher 捕获 → 重新解码 →
///   当前画布即时刷新 + 通过 <see cref="NoticeRequested"/> 通知视图弹 Snackbar。
///   「原图快照（基线）」是首次打开该资产时的解码结果，**不随热重载变化**，
///   因此对比帘展示的是「本次会话打开时 vs 磁盘最新」的真实差异。
/// </summary>
public sealed partial class MediaViewModel : ObservableObject, IDisposable
{
    private readonly LocalImageLoader _loader = new();
    private FileSystemWatcher? _watcher;
    private CancellationTokenSource? _thumbCts;

    /// <summary>当前画布资产的「原图快照」（首次打开时冻结的解码结果）。</summary>
    private BitmapSource? _baselineImage;

    public MediaViewModel()
    {
        AssetsView = CollectionViewSource.GetDefaultView(Assets);
        AssetsView.Filter = FilterAsset;
    }

    /// <summary>当前目录下的全部受支持资产。</summary>
    public ObservableCollection<MediaAssetItemViewModel> Assets { get; } = [];

    /// <summary>资产墙的筛选视图（检索框 + 状态筛选的组合结果）。</summary>
    public ICollectionView AssetsView { get; }

    // ---------------------------------------------------------------- 目录

    /// <summary>当前工作目录（null = 未选择）。</summary>
    [ObservableProperty]
    public partial string? FolderPath { get; set; }

    /// <summary>目录标签（空态提示用）。</summary>
    [ObservableProperty]
    public partial string FolderLabel { get; set; } = "尚未选择资产目录";

    /// <summary>列表统计标签。</summary>
    [ObservableProperty]
    public partial string AssetCountLabel { get; set; } = "0 项";

    /// <summary>是否已载入目录。</summary>
    [ObservableProperty]
    public partial bool HasFolder { get; set; }

    /// <summary>受支持扩展名提示（来自解码服务的唯一真源）。</summary>
    public string SupportedFormatsLabel => LocalImageLoader.SupportedExtensionLabel;

    // ---------------------------------------------------------------- 检索

    /// <summary>检索词：即时匹配文件名、状态文案与分辨率标签。</summary>
    [ObservableProperty]
    public partial string SearchText { get; set; } = string.Empty;

    /// <summary>状态筛选。</summary>
    [ObservableProperty]
    public partial MediaStatusFilter StatusFilter { get; set; } = MediaStatusFilter.All;

    /// <summary>当前筛选摘要（筛选按钮文案）。</summary>
    public string StatusFilterLabel => StatusFilter switch
    {
        MediaStatusFilter.Untouched => "只看：未修改",
        MediaStatusFilter.Editing => "只看：修图中",
        MediaStatusFilter.Completed => "只看：已完成",
        _ => "全部状态",
    };

    /// <summary>检索/筛选结果为空（目录里确实有资产，只是被筛掉）时显示的空态。</summary>
    public bool IsFilterEmpty => HasFolder && Assets.Count > 0 && AssetsView.IsEmpty;

    /// <summary>目录内一张受支持图片都没有。</summary>
    public bool IsWallEmpty => HasFolder && Assets.Count == 0;

    /// <summary>尚未选择目录（首屏引导态）。</summary>
    public bool IsNoFolder => !HasFolder;

    // ---------------------------------------------------------------- 选中与画布

    /// <summary>当前画布上的资产。</summary>
    [ObservableProperty]
    public partial MediaAssetItemViewModel? SelectedAsset { get; set; }

    /// <summary>画布主图（磁盘最新内容）。</summary>
    [ObservableProperty]
    public partial BitmapSource? CurrentImage { get; set; }

    /// <summary>原图快照（对比帘的左侧层）。</summary>
    [ObservableProperty]
    public partial BitmapSource? BaselineImageForView { get; set; }

    /// <summary>画布是否可呈现内容。</summary>
    [ObservableProperty]
    public partial bool HasImage { get; set; }

    /// <summary>当前资产标签（含分辨率）。</summary>
    [ObservableProperty]
    public partial string CurrentAssetLabel { get; set; } = "未选择资产";

    /// <summary>热重载次数（同一资产的外部保存计数，UI 上可见地累积）。</summary>
    [ObservableProperty]
    public partial int HotReloadCount { get; set; }

    // ---------------------------------------------------------------- 视图参数

    /// <summary>缩放百分比（10–800，画布几何由此派生）。</summary>
    [ObservableProperty]
    public partial double ZoomPercent { get; set; } = 100;

    /// <summary>透明通道棋盘底。</summary>
    [ObservableProperty]
    public partial bool IsCheckerboardVisible { get; set; } = true;

    /// <summary>对比形态。</summary>
    [ObservableProperty]
    public partial MediaCompareMode CompareMode { get; set; } = MediaCompareMode.None;

    /// <summary>滑动帘分割线位置（百分比 2–98，避开边界死区）。</summary>
    [ObservableProperty]
    public partial double CompareSplitPercent { get; set; } = 50;

    /// <summary>缩放比例（LayoutTransform 的唯一输入）。</summary>
    public double ZoomScale => ZoomPercent / 100.0;

    public string ZoomLabel => $"{ZoomPercent:0}%";

    public bool IsSingleMode => CompareMode == MediaCompareMode.None;

    public bool IsCurtainMode => CompareMode == MediaCompareMode.Curtain;

    public bool IsSplitMode => CompareMode == MediaCompareMode.Split;

    public bool IsCurtainOrSplit => CompareMode is MediaCompareMode.Curtain or MediaCompareMode.Split;

    public bool IsComparing => CompareMode != MediaCompareMode.None;

    public string CompareModeLabel => CompareMode switch
    {
        MediaCompareMode.Curtain => "对比：滑动帘",
        MediaCompareMode.Split => "对比：分屏",
        _ => "对比：关闭",
    };

    /// <summary>基线是否可用（无基线时对比帘退化为单图）。</summary>
    public bool HasBaseline => _baselineImage is not null;

    /// <summary>基线提示文案。</summary>
    public string BaselineHint => HasBaseline
        ? "原图快照 = 本次会话首次打开时的内容"
        : "尚无原图快照";

    // ---------------------------------------------------------------- 跨层通知

    /// <summary>
    /// 面向视图的提示通道（由 MediaPage 订阅并转为 Snackbar）。
    /// 参数：标题、正文、是否错误态。VM 不直接碰任何 UI 控件。
    /// </summary>
    public event Action<string, string, bool>? NoticeRequested;

    // ---------------------------------------------------------------- 命令

    /// <summary>选择资产目录（本地文件夹对话框）。</summary>
    [RelayCommand]
    private void OpenFolder()
    {
        var dialog = new OpenFolderDialog
        {
            Title = "选择视觉资产目录",
            Multiselect = false,
        };
        if (dialog.ShowDialog() == true && !string.IsNullOrWhiteSpace(dialog.FolderName))
        {
            LoadFolder(dialog.FolderName);
        }
    }

    /// <summary>重新扫描当前目录（保持选中项）。</summary>
    [RelayCommand]
    private void Refresh() => LoadFolder(FolderPath);

    /// <summary>手动重新解码当前资产（等价于一次热重载）。</summary>
    [RelayCommand]
    private void ReloadSelected()
    {
        if (SelectedAsset is null)
        {
            return;
        }
        SelectedAsset.Touch();
        ReloadImages(SelectedAsset, resetBaseline: false);
        NoticeRequested?.Invoke("已重新载入", SelectedAsset.FileName, false);
    }

    /// <summary>重新抓取原图快照（接受当前磁盘内容为新基线）。</summary>
    [RelayCommand]
    private void ResetBaseline()
    {
        if (SelectedAsset is null)
        {
            return;
        }
        ReloadImages(SelectedAsset, resetBaseline: true);
        NoticeRequested?.Invoke("原图快照已重置", SelectedAsset.FileName, false);
    }

    /// <summary>切换对比形态（参数：None / Curtain / Split）。</summary>
    [RelayCommand]
    private void SetCompareMode(string? mode)
    {
        CompareMode = Enum.TryParse<MediaCompareMode>(mode, ignoreCase: true, out var parsed)
            ? parsed
            : MediaCompareMode.None;

        // 对比形态只看「已冻结的两层」：与单图缩放解耦，避免缩放造成的视差错觉
        if (IsComparing && ZoomPercent != 100)
        {
            ZoomPercent = 100;
        }
    }

    /// <summary>设置状态筛选（参数：All / Untouched / Editing / Completed）。</summary>
    [RelayCommand]
    private void SetStatusFilter(string? filter)
    {
        StatusFilter = Enum.TryParse<MediaStatusFilter>(filter, ignoreCase: true, out var parsed)
            ? parsed
            : MediaStatusFilter.All;
        RaiseFilterChanged();
    }

    /// <summary>把资产标记为「已完成」（资产卡片命令，参数为卡片自身）。</summary>
    [RelayCommand]
    private void MarkCompleted(MediaAssetItemViewModel? item) =>
        ApplyStatus(item ?? SelectedAsset, MediaAssetStatus.Completed);

    /// <summary>把资产标记为「修图中」（资产卡片命令）。</summary>
    [RelayCommand]
    private void MarkEditing(MediaAssetItemViewModel? item) =>
        ApplyStatus(item ?? SelectedAsset, MediaAssetStatus.Editing);

    /// <summary>条目级：在资源管理器中定位该资产（本地文件管理，零网络行为）。</summary>
    [RelayCommand]
    private void RevealAsset(MediaAssetItemViewModel? item) => Reveal(item ?? SelectedAsset);

    /// <summary>状态落库的唯一入口（卡片命令与选中项命令共用一条实现）。</summary>
    private void ApplyStatus(MediaAssetItemViewModel? item, MediaAssetStatus status)
    {
        if (item is null)
        {
            return;
        }
        item.Status = status;
        RaiseFilterChanged();
        NoticeRequested?.Invoke("状态已更新", $"{item.FileName} → {item.StatusLabel}", false);
    }

    private void Reveal(MediaAssetItemViewModel? item)
    {
        var target = item?.FullPath;
        if (string.IsNullOrEmpty(target) || !File.Exists(target))
        {
            NoticeRequested?.Invoke("无法定位", "文件已不存在或尚未选择资产", true);
            return;
        }
        try
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{target}\"")
            {
                UseShellExecute = true,
            });
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            NoticeRequested?.Invoke("打开资源管理器失败", ex.Message, true);
        }
    }

    [RelayCommand]
    private void ZoomIn() => ZoomPercent = Math.Min(800, ZoomPercent + 10);

    [RelayCommand]
    private void ZoomOut() => ZoomPercent = Math.Max(10, ZoomPercent - 10);

    [RelayCommand]
    private void ResetZoom() => ZoomPercent = 100;

    partial void OnSearchTextChanged(string value) => RaiseFilterChanged();

    partial void OnStatusFilterChanged(MediaStatusFilter value) => OnPropertyChanged(nameof(StatusFilterLabel));

    partial void OnSelectedAssetChanged(MediaAssetItemViewModel? value)
    {
        if (value is null)
        {
            CurrentImage = null;
            _baselineImage = null;
            BaselineImageForView = null;
            HasImage = false;
            CurrentAssetLabel = "未选择资产";
            RaiseBaselineChanged();
            return;
        }

        ReloadImages(value, resetBaseline: true);
    }

    partial void OnZoomPercentChanged(double value)
    {
        OnPropertyChanged(nameof(ZoomScale));
        OnPropertyChanged(nameof(ZoomLabel));
    }

    partial void OnCompareModeChanged(MediaCompareMode value)
    {
        OnPropertyChanged(nameof(IsSingleMode));
        OnPropertyChanged(nameof(IsCurtainMode));
        OnPropertyChanged(nameof(IsSplitMode));
        OnPropertyChanged(nameof(IsCurtainOrSplit));
        OnPropertyChanged(nameof(IsComparing));
        OnPropertyChanged(nameof(CompareModeLabel));
    }

    partial void OnFolderPathChanged(string? value) => HasFolder = value is not null;

    // ---------------------------------------------------------------- 载入

    /// <summary>载入目录：扫描受支持图片 → 重置状态 → 后台解码缩略图 → 布防监视器。</summary>
    public void LoadFolder(string? folder)
    {
        if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
        {
            NoticeRequested?.Invoke("目录不可用", folder ?? "（空路径）", true);
            return;
        }

        FolderPath = folder;
        FolderLabel = folder;
        SelectedAsset = null;
        Assets.Clear();

        IEnumerable<string> files;
        try
        {
            files = Directory.EnumerateFiles(folder)
                .Where(LocalImageLoader.IsSupported)
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            NoticeRequested?.Invoke("读取目录失败", ex.Message, true);
            return;
        }

        foreach (var file in files)
        {
            Assets.Add(new MediaAssetItemViewModel(file));
        }

        AssetCountLabel = $"{Assets.Count} 项 · 受支持 {SupportedFormatsLabel}";
        RaiseFilterChanged();
        StartWatcher(folder);
        StartThumbnailDecode();

        if (Assets.Count > 0)
        {
            SelectedAsset = Assets[0];
        }
        else
        {
            NoticeRequested?.Invoke("目录内没有受支持的图片", $"{folder}（受支持：{SupportedFormatsLabel}）", true);
        }
    }

    /// <summary>重新解码当前画布（<paramref name="resetBaseline"/> 为真时同时刷新原图快照）。</summary>
    private void ReloadImages(MediaAssetItemViewModel item, bool resetBaseline)
    {
        var image = _loader.Load(item.FullPath);
        if (image is null)
        {
            HasImage = false;
            CurrentImage = null;
            CurrentAssetLabel = $"{item.FileName} · 无法解码";
            NoticeRequested?.Invoke("解码失败", item.FileName, true);
            return;
        }

        CurrentImage = image;
        HasImage = true;
        CurrentAssetLabel = $"{item.FileName} · {LocalImageLoader.DescribePixelSize(image)}";

        if (resetBaseline || _baselineImage is null)
        {
            _baselineImage = image;
            BaselineImageForView = image;
            RaiseBaselineChanged();
        }
    }

    private void RaiseBaselineChanged()
    {
        OnPropertyChanged(nameof(HasBaseline));
        OnPropertyChanged(nameof(BaselineHint));
    }

    private void RaiseFilterChanged()
    {
        AssetsView.Refresh();
        OnPropertyChanged(nameof(IsFilterEmpty));
        OnPropertyChanged(nameof(IsWallEmpty));
        OnPropertyChanged(nameof(IsNoFolder));
        OnPropertyChanged(nameof(StatusFilterLabel));
    }

    private bool FilterAsset(object candidate)
    {
        if (candidate is not MediaAssetItemViewModel item)
        {
            return false;
        }

        if (StatusFilter != MediaStatusFilter.All
            && item.Status.ToString() != StatusFilter.ToString())
        {
            return false;
        }

        var query = SearchText.Trim();
        if (query.Length == 0)
        {
            return true;
        }

        return item.FileName.Contains(query, StringComparison.OrdinalIgnoreCase)
               || item.StatusLabel.Contains(query, StringComparison.OrdinalIgnoreCase)
               || item.MetaLabel.Contains(query, StringComparison.OrdinalIgnoreCase)
               || item.PixelSizeLabel.Contains(query, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// 缩略图后台解码：串行 + 可取消（切换目录立即终止上一批），
    /// 解码在后台线程完成、赋值回 UI 线程。
    /// </summary>
    private void StartThumbnailDecode()
    {
        _thumbCts?.Cancel();
        _thumbCts = new CancellationTokenSource();
        var token = _thumbCts.Token;
        var snapshot = Assets.ToList();

        _ = Task.Run(() =>
        {
            foreach (var item in snapshot)
            {
                if (token.IsCancellationRequested)
                {
                    return;
                }
                var thumb = _loader.Load(item.FullPath, decodePixelWidth: 220);
                if (token.IsCancellationRequested)
                {
                    return;
                }
                var dispatcher = Application.Current?.Dispatcher;
                if (dispatcher is null)
                {
                    item.ApplyThumbnail(thumb);
                    continue;
                }
                dispatcher.InvokeAsync(() => item.ApplyThumbnail(thumb));
            }
        }, token);
    }

    // ---------------------------------------------------------------- 热重载

    /// <summary>
    /// 布防目录监视：外部覆盖保存（LastWrite/Size/FileName）即触发重新解码。
    /// 监视器本身不带任何跨进程通信，纯本地文件系统事件。
    /// </summary>
    private void StartWatcher(string folder)
    {
        _watcher?.Dispose();
        try
        {
            _watcher = new FileSystemWatcher(folder)
            {
                NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.FileName,
                IncludeSubdirectories = false,
                EnableRaisingEvents = true,
            };
            _watcher.Changed += OnWatched;
            _watcher.Created += OnWatched;
            _watcher.Renamed += OnWatchedRename;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
        {
            _watcher = null;
            NoticeRequested?.Invoke("热重载监视未能布防", $"{folder}：{ex.Message}", true);
        }
    }

    private void OnWatchedRename(object sender, RenamedEventArgs e)
    {
        var dispatcher = Application.Current?.Dispatcher;
        dispatcher?.InvokeAsync(() => HandleDiskChange(e.FullPath, isNew: true));
    }

    private void OnWatched(object sender, FileSystemEventArgs e)
    {
        var dispatcher = Application.Current?.Dispatcher;
        dispatcher?.InvokeAsync(() => HandleDiskChange(e.FullPath, isNew: e.ChangeType == WatcherChangeTypes.Created));
    }

    /// <summary>
    /// 处理一次磁盘变更：新增文件入墙；已存在文件刷新体积/时间戳并按需重解码画布。
    /// 外部软件写文件常触发多次事件，这里以「路径 + 内容是否真的变了」做幂等收敛。
    /// </summary>
    internal void HandleDiskChange(string path, bool isNew)
    {
        if (!LocalImageLoader.IsSupported(path))
        {
            return;
        }

        var existing = Assets.FirstOrDefault(
            a => string.Equals(a.FullPath, path, StringComparison.OrdinalIgnoreCase));

        if (existing is null)
        {
            var item = new MediaAssetItemViewModel(path);
            Assets.Add(item);
            item.ApplyThumbnail(_loader.Load(path, decodePixelWidth: 220));
            AssetCountLabel = $"{Assets.Count} 项 · 受支持 {SupportedFormatsLabel}";
            RaiseFilterChanged();
            NoticeRequested?.Invoke("发现新资产", item.FileName, false);
            return;
        }

        if (!isNew && !File.Exists(path))
        {
            return;
        }

        var beforeSize = existing.SizeBytes;
        existing.Touch();
        if (existing.SizeBytes == beforeSize && !ReferenceEquals(existing, SelectedAsset))
        {
            return;
        }

        existing.ApplyThumbnail(_loader.Load(path, decodePixelWidth: 220));

        if (ReferenceEquals(existing, SelectedAsset))
        {
            // 覆盖保存必须重新读盘：解码服务的缓存键含 mtime/size，天然穿透
            CurrentImage = _loader.Load(existing.FullPath);
            HasImage = CurrentImage is not null;
            CurrentAssetLabel = $"{existing.FileName} · {LocalImageLoader.DescribePixelSize(CurrentImage)}";
            HotReloadCount++;
            NoticeRequested?.Invoke(
                "外部保存已热重载",
                $"{existing.FileName}（第 {HotReloadCount} 次）· {existing.SizeLabel}",
                false);
        }
        else
        {
            NoticeRequested?.Invoke("资产已更新", existing.FileName, false);
        }
    }

    public void Dispose()
    {
        _thumbCts?.Cancel();
        _thumbCts?.Dispose();
        _thumbCts = null;
        _watcher?.Dispose();
        _watcher = null;
    }
}
