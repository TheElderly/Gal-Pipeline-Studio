using System.Windows.Controls;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Pages;

/// <summary>
/// LQA Lab 质检中心宿主：进入页面即从工坊单例重建看板，
/// 保证看到的是此刻的实时违例状态（停留期间可手动刷新）。
/// </summary>
public partial class LqaLabPage : Page
{
    private readonly LqaLabViewModel _vm = new();

    public LqaLabPage()
    {
        InitializeComponent();
        DataContext = _vm;
        Loaded += (_, _) => _vm.RefreshBoardCommand.Execute(null);
    }
}
