using System.Windows.Controls;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Views;

/// <summary>Settings 页宿主：每次导航重建视图与视图模型（MVVM 标准 DataContext 装配）。</summary>
public partial class SettingsView : UserControl
{
    public SettingsView()
    {
        InitializeComponent();
        DataContext = new SettingsViewModel();
    }
}
