using System.Windows;
using Wpf.Ui.Abstractions;

namespace GalPipeline.Desktop.Services;

/// <summary>
/// NavigationView 页面工厂：INavigationViewPageProvider 最小实现。
/// 无缓存、激活即新建（页面均为无状态轻量占位/工作台视图）。
/// </summary>
public sealed class PageProviderService : INavigationViewPageProvider
{
    public object? GetPage(Type pageType) =>
        Activator.CreateInstance(pageType);
}
