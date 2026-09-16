using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Views;

/// <summary>Studio 工坊宿主：Ctrl+F 聚焦搜索，译文失焦回写 DTO，支持剧本拖入。</summary>
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

    /// <summary>文件拖入：提取首个文件路径并走统一载入链（Detect → Extract）。</summary>
    private async void OnStudioViewDrop(object sender, DragEventArgs e)
    {
        if (e.Data.GetDataPresent(DataFormats.FileDrop))
        {
            var files = (string[]?)e.Data.GetData(DataFormats.FileDrop);
            if (files is { Length: > 0 } && DataContext is StudioViewModel vm)
            {
                await vm.LoadFileCommand.ExecuteAsync(files[0]);
            }
        }
    }
}
