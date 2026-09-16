using System.Windows;
using System.Windows.Controls;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Views;

/// <summary>Studio 工坊宿主：每次导航重建视图与视图模型（与 SettingsView 同纪律）。</summary>
public partial class StudioView : UserControl
{
    public StudioView()
    {
        InitializeComponent();
        DataContext = new StudioViewModel();
    }

    /// <summary>译文编辑失焦即回写 DTO（保证批量合并与后续导出取到最新值）。</summary>
    private void OnTranslationLostFocus(object sender, RoutedEventArgs e)
    {
        if (sender is TextBox textBox)
        {
            (textBox.DataContext as StudioUnitItemViewModel)?.CommitEdits();
        }
    }
}
