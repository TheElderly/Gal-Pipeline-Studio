using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using GalPipeline.Desktop.Pages;
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
        DataContext = _vm;
        Loaded += OnLoaded;
        Closed += (_, _) => _vm.Dispose();
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        // SelectedItem 的 setter 受保护：经条目自身 IsSelected 触发选中路由
        // （NavigationViewItem : ListViewItem，IsSelected 可写），统一走导航链。
        RootNavigation.MenuItems
            .OfType<NavigationViewItem>()
            .First(item => (string?)item.Tag == "studio")
            .IsActive = true;
        await _vm.InitializeAsync();
    }

    /// <summary>导航槽切换：按 Tag 路由到对应页并同步状态栏页标题。</summary>
    private void OnNavigationSelectionChanged(NavigationView sender, RoutedEventArgs args)
    {
        if (sender.SelectedItem is not NavigationViewItem { Tag: string tag })
        {
            return;
        }
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
