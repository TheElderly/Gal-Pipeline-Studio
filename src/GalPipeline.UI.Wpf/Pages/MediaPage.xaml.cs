using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using GalPipeline.Desktop.ViewModels;
using Wpf.Ui;
using Wpf.Ui.Controls;

namespace GalPipeline.Desktop.Pages;

/// <summary>
/// Media 视觉修图工作台宿主。
///
/// 视图层职责被刻意压到两条（其余全部在 <see cref="MediaViewModel"/>）：
///   1. Snackbar 呈现（宿主事务：服务需要一根 SnackbarPresenter）；
///   2. 滑动帘裁剪矩形与分割线位置（纯几何计算，坐标只存在于视觉树里）。
/// </summary>
public partial class MediaPage : Page
{
    private readonly MediaViewModel _vm = new();
    private readonly SnackbarService _snackbar = new();

    public MediaPage()
    {
        InitializeComponent();
        DataContext = _vm;

        _snackbar.SetSnackbarPresenter(MediaSnackbarPresenter);
        _vm.NoticeRequested += OnNoticeRequested;
        _vm.PropertyChanged += OnViewModelPropertyChanged;

        Loaded += (_, _) => UpdateCurtainGeometry();
        Unloaded += OnUnloaded;
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        _vm.NoticeRequested -= OnNoticeRequested;
        _vm.PropertyChanged -= OnViewModelPropertyChanged;
        _vm.Dispose();   // 停掉 FileSystemWatcher 与后台缩略图解码
    }

    /// <summary>VM 提示 → 平滑 Snackbar（成功/错误两态，杜绝弹窗）。</summary>
    private void OnNoticeRequested(string title, string message, bool isError)
    {
        _snackbar.Show(
            title,
            message,
            isError ? ControlAppearance.Danger : ControlAppearance.Success,
            new SymbolIcon(isError ? SymbolRegular.Warning24 : SymbolRegular.Checkmark24),
            TimeSpan.FromSeconds(3));
    }

    private void OnViewModelPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(MediaViewModel.CompareSplitPercent)
            or nameof(MediaViewModel.IsCurtainMode)
            or nameof(MediaViewModel.BaselineImageForView))
        {
            UpdateCurtainGeometry();
        }
    }

    private void OnCurtainHostSizeChanged(object sender, SizeChangedEventArgs e) => UpdateCurtainGeometry();

    /// <summary>拖拽分割线：位移换算为百分比后写回 VM（状态仍在 VM，视图只负责换算）。</summary>
    private void OnCurtainThumbDragDelta(object sender, DragDeltaEventArgs e)
    {
        var width = CurtainHost.ActualWidth;
        if (width <= 0)
        {
            return;
        }
        _vm.CompareSplitPercent = Math.Clamp(
            _vm.CompareSplitPercent + (e.HorizontalChange / width * 100.0), 2, 98);
        UpdateCurtainGeometry();
    }

    /// <summary>
    /// 依据帘幕位置刷新「原图快照」层的裁剪矩形与分割线：裁剪宽度 =
    /// 画布宽度 × 百分比，坐标系即 Image 元素自身（Stretch=Uniform 铺满宿主）。
    /// </summary>
    private void UpdateCurtainGeometry()
    {
        var width = CurtainHost.ActualWidth;
        var height = CurtainHost.ActualHeight;
        if (width <= 0 || height <= 0)
        {
            return;
        }

        var x = width * (_vm.CompareSplitPercent / 100.0);
        CurtainClip.Rect = new Rect(0, 0, Math.Max(0, x), height);
        CurtainLine.X1 = x;
        CurtainLine.X2 = x;
        CurtainLine.Y1 = 0;
        CurtainLine.Y2 = height;
        Canvas.SetLeft(CurtainThumb, x - (CurtainThumb.Width / 2));
        Canvas.SetTop(CurtainThumb, (height - CurtainThumb.Height) / 2);
    }
}
