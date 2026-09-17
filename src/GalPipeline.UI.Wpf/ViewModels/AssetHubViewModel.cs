using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.IPC;
using GalPipeline.Core.Project;
using GalPipeline.Desktop.Services;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>资产看板绑定项（含分类与 TLG 转换标记）。</summary>
public sealed partial class AssetEntryViewModel : ObservableObject
{
    public AssetEntryViewModel(AssetEntry entry) => Entry = entry;

    public AssetEntry Entry { get; }

    public string RelativePath => Entry.RelativePath;
    public string DisplayName => Path.GetFileName(Entry.FullPath);
    public string SizeText => $"{Entry.SizeBytes:#,0} B";

    /// <summary>私有格式（TLG）需要经转换管道才能预览。</summary>
    public bool NeedsConversion =>
        Entry.Extension.Equals(".tlg", StringComparison.OrdinalIgnoreCase);

    /// <summary>看板行的分类标签（与工作区目录约定同名）。</summary>
    public string KindLabel => Entry.Kind;
}

/// <summary>
/// Asset Hub 资产归档视图模型 —— 工作区资产浏览器。
///
/// 数据源 = 工坊单例的 GameDirectory 推导出的 ``.galpipeline/raw`` 工作区
/// （扫描纯函数在 GalPipeline.Core.Project.AssetWorkspace，dotnet test 射程内）。
/// 双击交互：脚本 → 载入工坊审校；TLG → 经 convert_image 转入缓存后用系统
/// 查看器打开；其余图片/音频 → 系统默认查看器。
/// </summary>
public sealed partial class AssetHubViewModel : ObservableObject
{
    /// <summary>TLG 预览缓存根：与 FFmpeg 音频缓存同一处约定（%TEMP%/galpipeline/...）。</summary>
    public static string TlgCacheDirectory =>
        Path.Combine(Path.GetTempPath(), "galpipeline", "cache", "tlg");

    public ObservableCollection<AssetEntryViewModel> Scripts { get; } = [];
    public ObservableCollection<AssetEntryViewModel> Images { get; } = [];
    public ObservableCollection<AssetEntryViewModel> Audio { get; } = [];
    public ObservableCollection<LqaRuleSummaryLike> KindSummaries { get; } = [];

    /// <summary>看板统计徽章的轻量载体（避免为计数再建 record）。</summary>
    public sealed record LqaRuleSummaryLike(string Label, int Count);

    [ObservableProperty]
    public partial string WorkspaceLabel { get; set; } = string.Empty;

    [ObservableProperty]
    public partial bool HasWorkspace { get; set; }

    [ObservableProperty]
    public partial string? PreviewPath { get; set; }

    [ObservableProperty]
    public partial string PreviewText { get; set; } = string.Empty;

    [ObservableProperty]
    public partial bool IsConvertingPreview { get; set; }

    /// <summary>预览图像源（OnDemand 加载并立即释放文件句柄，不锁资产文件）。</summary>
    public System.Windows.Media.ImageSource? PreviewSource
    {
        get
        {
            if (PreviewPath is null || !File.Exists(PreviewPath))
            {
                return null;
            }
            var bitmap = new System.Windows.Media.Imaging.BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = System.Windows.Media.Imaging.BitmapCacheOption.OnLoad;
            bitmap.UriSource = new Uri(PreviewPath);
            bitmap.EndInit();
            bitmap.Freeze();
            return bitmap;
        }
    }

    partial void OnPreviewPathChanged(string? value) => OnPropertyChanged(nameof(PreviewSource));

    private int _previewRequestId;

    partial void OnHasWorkspaceChanged(bool value)
    {
        OnPropertyChanged(nameof(BoardVisibility));
        OnPropertyChanged(nameof(EmptyWorkspaceVisibility));
    }

    public Visibility BoardVisibility => HasWorkspace ? Visibility.Visible : Visibility.Collapsed;
    public Visibility EmptyWorkspaceVisibility => HasWorkspace ? Visibility.Collapsed : Visibility.Visible;

    private static string? RawRoot =>
        StudioViewModelProvider.Shared.GameDirectory is { } game
            ? Path.Combine(game, ".galpipeline", "raw")
            : null;

    /// <summary>从工坊单例的当前工程重建资产看板。</summary>
    [RelayCommand]
    public void RefreshBoard()
    {
        Scripts.Clear();
        Images.Clear();
        Audio.Clear();
        KindSummaries.Clear();

        var raw = RawRoot;
        HasWorkspace = raw is not null && Directory.Exists(raw);
        WorkspaceLabel = HasWorkspace
            ? $"工作区：{Path.Combine(StudioViewModelProvider.Shared.GameDirectory!, ".galpipeline", "raw")}"
            : "尚未初始化工作区 —— 请先在剧本工坊拖入游戏根目录";

        if (!HasWorkspace)
        {
            PreviewPath = null;
            PreviewText = string.Empty;
            return;
        }

        var snapshot = AssetWorkspace.Scan(raw);
        foreach (var entry in snapshot.Scripts)
        {
            Scripts.Add(new AssetEntryViewModel(entry));
        }
        foreach (var entry in snapshot.Images)
        {
            Images.Add(new AssetEntryViewModel(entry));
        }
        foreach (var entry in snapshot.Audio)
        {
            Audio.Add(new AssetEntryViewModel(entry));
        }

        KindSummaries.Add(new LqaRuleSummaryLike($"剧本 {snapshot.Scripts.Count}", snapshot.Scripts.Count));
        KindSummaries.Add(new LqaRuleSummaryLike($"图像 {snapshot.Images.Count}", snapshot.Images.Count));
        KindSummaries.Add(new LqaRuleSummaryLike($"音频 {snapshot.Audio.Count}", snapshot.Audio.Count));
        if (snapshot.Truncated)
        {
            KindSummaries.Add(new LqaRuleSummaryLike("已截断（超出单类上限）", 0));
        }
    }

    /// <summary>打开工作区资产目录（资源管理器）。</summary>
    [RelayCommand]
    private void OpenAssetFolder()
    {
        var raw = RawRoot;
        if (raw is null || !Directory.Exists(raw))
        {
            return;
        }
        Directory.CreateDirectory(raw);
        Process.Start(new ProcessStartInfo(raw) { UseShellExecute = true });
    }

    /// <summary>刷新工程树（解包完成后由工坊侧联动触发）。</summary>
    [RelayCommand]
    private void RefreshProjectTree() =>
        StudioViewModelProvider.Shared.RefreshProjectTreeCommand.Execute(null);

    /// <summary>
    /// 双击资产：脚本 → 载入工坊；TLG → 转缓存后打开；其余 → 系统查看器。
    /// </summary>
    [RelayCommand]
    public async Task OpenAssetAsync(AssetEntryViewModel? item)
    {
        if (item is null || !File.Exists(item.Entry.FullPath))
        {
            return;
        }

        if (item.Entry.Kind == "scripts")
        {
            // 剧本双击的语义 =「在工坊中打开」：走统一载入链（防丢失与筛选复位齐备）
            await StudioViewModelProvider.Shared.LoadFileCommand.ExecuteAsync(item.Entry.FullPath);
            StudioViewModelProvider.RaiseNavigateRequested("studio");
            return;
        }

        if (item.NeedsConversion)
        {
            var png = await ConvertToCacheAsync(item.Entry.FullPath);
            if (png is not null)
            {
                Process.Start(new ProcessStartInfo(png) { UseShellExecute = true });
            }
            return;
        }

        Process.Start(new ProcessStartInfo(item.Entry.FullPath) { UseShellExecute = true });
    }

    /// <summary>选中图像条目时的即插即看预览（TLG 自动转入缓存）。</summary>
    public async Task LoadPreviewAsync(AssetEntryViewModel? item)
    {
        var requestId = ++_previewRequestId;
        if (item is null || item.Entry.Kind != "images")
        {
            PreviewPath = null;
            PreviewText = string.Empty;
            return;
        }

        try
        {
            if (item.NeedsConversion)
            {
                IsConvertingPreview = true;
                PreviewText = "正在转换 TLG → PNG（缓存）…";
                var png = await ConvertToCacheAsync(item.Entry.FullPath);
                if (_previewRequestId != requestId)
                {
                    return; // 用户已切走：迟到的转换直接丢弃
                }
                if (png is null)
                {
                    return; // 失败原因已写 PreviewText
                }
                PreviewPath = png;
                PreviewText = $"已转换：{item.DisplayName}";
            }
            else
            {
                PreviewPath = item.Entry.FullPath;
                PreviewText = item.DisplayName;
            }
        }
        finally
        {
            if (_previewRequestId == requestId)
            {
                IsConvertingPreview = false;
            }
        }
    }

    /// <summary>
    /// TLG → 缓存 PNG（确定性指纹命名，源变更即失效）。失败原因写入
    /// PreviewText（含 TLG6 未装配外部工具的装配引导），返回 null 表示失败。
    /// </summary>
    private async Task<string?> ConvertToCacheAsync(string tlgPath)
    {
        var dst = AssetWorkspace.TlgCachePathFor(tlgPath, TlgCacheDirectory);
        if (File.Exists(dst))
        {
            return dst; // 缓存命中：零 RPC 开销
        }
        try
        {
            var result = await SidecarClientProvider.Client.ConvertImageAsync(tlgPath, dst);
            return result.DstPath;
        }
        catch (Exception ex)
        {
            PreviewText = $"转换失败：{ex.Message}";
            return null;
        }
    }
}
