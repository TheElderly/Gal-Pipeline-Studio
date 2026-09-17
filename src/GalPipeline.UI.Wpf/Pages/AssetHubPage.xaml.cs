using System.Windows.Controls;
using System.Windows.Input;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Pages;

/// <summary>
/// Asset Hub 资产归档宿主：进入页面即重扫工作区；
/// 列表双击走 OpenAsset（脚本载入工坊 / TLG 转缓存预览 / 其余系统查看器），
/// 图像选中即触发即插即看预览。
/// </summary>
public partial class AssetHubPage : Page
{
    private readonly AssetHubViewModel _vm = new();

    public AssetHubPage()
    {
        InitializeComponent();
        DataContext = _vm;
        Loaded += (_, _) => _vm.RefreshBoardCommand.Execute(null);
    }

    private void OnAssetDoubleClicked(object sender, MouseButtonEventArgs e)
    {
        if ((sender as ListView)?.SelectedItem is AssetEntryViewModel item)
        {
            _ = _vm.OpenAssetCommand.ExecuteAsync(item);
        }
    }

    private void OnAssetSelected(object sender, SelectionChangedEventArgs e)
    {
        if ((sender as ListView)?.SelectedItem is AssetEntryViewModel item)
        {
            _ = _vm.LoadPreviewAsync(item);
        }
    }
}
