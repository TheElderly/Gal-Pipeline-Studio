using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Views;

/// <summary>Studio 工坊宿主：Ctrl+F 聚焦搜索，译文失焦即回写 DTO。</summary>
public partial class StudioView : UserControl
{
    public StudioView()
    {
        InitializeComponent();
        DataContext = new StudioViewModel();
    }

    /// <summary>Ctrl+F 聚焦搜索框（设计稿的 Search line / Ctrl+F 快捷键）。</summary>
    private void OnStudioPreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.F && Keyboard.Modifiers.HasFlag(ModifierKeys.Control))
        {
            SearchBox.Focus();
            SearchBox.SelectAll();
            e.Handled = true;
        }
    }

    /// <summary>译文编辑失焦即回写 DTO（保证批量合并与导出取到最新值）。</summary>
    private void OnTranslationLostFocus(object sender, RoutedEventArgs e)
    {
        if (sender is TextBox textBox)
        {
            (textBox.DataContext as StudioUnitItemViewModel)?.CommitEdits();
        }
    }
}
