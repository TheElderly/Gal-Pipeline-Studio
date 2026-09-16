using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using GalPipeline.Desktop.Pages;
using GalPipeline.Desktop.Services;
using GalPipeline.Desktop.ViewModels;
using Wpf.Ui.Controls;

namespace GalPipeline.Desktop;

/// <summary>主壳窗口：NavigationView 四槽导航 + 底部全局状态栏。</summary>
public sealed partial class MainWindow : FluentWindow
{
    private readonly ShellViewModel _vm = new();

    public MainWindow()
    {
        InitializeComponent();
        // 选中/调用依赖 TargetPageType + 页面服务（缺服务时点击选择会被回退）
        RootNavigation.SetPageProviderService(new PageProviderService());
        StudioViewModel.MetricsSink = (file, metrics) => _vm.PublishStudioMetrics(file, metrics);
        DataContext = _vm;
        Loaded += OnLoaded;
        Closed += (_, _) => _vm.Dispose();
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        // WPF-UI 4.3 的 NavigationViewItem 点击事件链（ItemInvoked /
        // SelectionChanged）在本宿主下不触发（实测探针无日志），
        // 故导航统一由 OnPaneMouseUp 命中测试驱动；启动以首槽等效打开。
        NavigateTo("studio");
        MarkActive("studio");
        await _vm.InitializeAsync();
    }

    /// <summary>
    /// 侧栏命中路由：MouseUp（冒泡）在 NavigationView 上必然触达，
    /// 对命中点做可视树上溯，找到 NavigationViewItem 即导航。
    /// </summary>
    private void OnPaneMouseUp(object sender, MouseButtonEventArgs e)
    {
        var navigationView = (NavigationView)sender;
        var origin = e.GetPosition(navigationView);
        var hit = VisualTreeHelper.HitTest(navigationView, origin);
        if (hit?.VisualHit is null)
        {
            return;
        }
        DependencyObject? current = hit.VisualHit;
        while (current is not null)
        {
            if (current is NavigationViewItem { Tag: string tag })
            {
                NavigateTo(tag);
                MarkActive(tag);
                return;
            }
            current = current is Visual or System.Windows.Media.Media3D.Visual3D
                ? VisualTreeHelper.GetParent(current)
                : LogicalTreeHelper.GetParent(current);
        }
    }

    private void MarkActive(string tag)
    {
        foreach (var item in RootNavigation.MenuItems.OfType<NavigationViewItem>()
                     .Concat(RootNavigation.FooterMenuItems.OfType<NavigationViewItem>()))
        {
            item.IsActive = (string?)item.Tag == tag;
        }
    }

    private void NavigateTo(string tag)
    {
        Page page = tag switch
        {
            "studio" => new StudioPage(),
            "assets" => new AssetHubPage(),
            "lqa" => new LqaLabPage(),
            "settings" => new SettingsPage(),
            _ => new StudioPage(),
        };
        ContentFrame.Navigate(page);
        _vm.CurrentPageTitle = tag switch
        {
            "studio" => "Studio 剧本工坊",
            "assets" => "Asset Hub 资产归档",
            "lqa" => "LQA Lab 质检中心",
            "settings" => "Settings 模型与后端设置",
            _ => "Studio 剧本工坊",
        };
    }
}
