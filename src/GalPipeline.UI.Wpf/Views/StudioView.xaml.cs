using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using GalPipeline.Desktop.Services;
using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Views;

/// <summary>
/// Studio 工坊宿主：Ctrl+F 聚焦搜索、译文失焦回写 DTO 并即时复检 LQA、
/// 支持剧本拖入、启动自动载入内置真实样本（不再是硬编码演示数据）。
/// VM 为进程级共享单例（StudioViewModelProvider.Shared）—— 跨导航保留
/// 载入与审校状态，LQA Lab 看板 / 定位 / 面板内重译才能操作同一份真相。
/// </summary>
public partial class StudioView : UserControl
{
    private readonly StudioViewModel _vm = StudioViewModelProvider.Shared;

    public StudioView()
    {
        InitializeComponent();
        _vm.FiltersReset += OnFiltersReset;
        DataContext = _vm;
        Loaded += OnStudioViewLoaded;
        Unloaded += OnStudioViewUnloaded;
    }

    /// <summary>
    /// 启动就绪后自动载入内置 KAG 样本，走真实 RPC 链路（detect → extract）。
    /// 单例 VM 内部幂等（只载一次）；页面被 Frame 重新导航时不重复拉取。
    /// </summary>
    private async void OnStudioViewLoaded(object sender, RoutedEventArgs e)
    {
        await _vm.InitializeAsync();
    }

    /// <summary>实例随导航丢弃：必须退订单例 VM 的实例级事件，否则旧视图泄漏。</summary>
    private void OnStudioViewUnloaded(object sender, RoutedEventArgs e)
    {
        _vm.FiltersReset -= OnFiltersReset;
    }

    /// <summary>
    /// 筛选胶囊选中态归位：胶囊由 Command 驱动而非 IsChecked 双向绑定，
    /// 故 VM 侧重置 FilterMode 后须由视图把 RadioButton 勾回 All
    /// （同容器内 RadioButton 自动互斥，无需手动取消其他项）。
    /// </summary>
    private void OnFiltersReset() => FilterAll.IsChecked = true;

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

    /// <summary>
    /// 译文编辑失焦：先回写 DTO（保证批量合并与导出取到最新值），
    /// 再送 Sidecar 重跑静态 LQA 规则，即时刷新该行状态胶囊与错误气泡。
    /// 两条链路都必须保留 —— 只 CommitEdits 不复检，改坏的译文永远不会被拦下。
    /// </summary>
    private async void OnTranslationLostFocus(object sender, RoutedEventArgs e)
    {
        if (sender is not TextBox textBox
            || textBox.DataContext is not StudioUnitItemViewModel item)
        {
            return;
        }
        item.CommitEdits();
        await _vm.RecheckUnitAsync(item);
    }

    /// <summary>
    /// 拖入分发：游戏根**目录** → 工程模式（引擎识别 + 剧本树）；
    /// 单个剧本文件 → 既有单文件载入链。目录与文件走不同命令，严禁混流。
    /// </summary>
    private async void OnStudioViewDrop(object sender, DragEventArgs e)
    {
        if (!e.Data.GetDataPresent(DataFormats.FileDrop))
        {
            return;
        }
        var paths = (string[]?)e.Data.GetData(DataFormats.FileDrop);
        if (paths is not { Length: > 0 })
        {
            return;
        }
        if (Directory.Exists(paths[0]))
        {
            await _vm.LoadGameDirectoryCommand.ExecuteAsync(paths[0]);
        }
        else
        {
            await _vm.LoadFileCommand.ExecuteAsync(paths[0]);
        }
    }

    /// <summary>
    /// 工程树点击切换剧本：选中即消费（复位选中态以允许再次点击同项重载），
    /// 切换命令在 VM 侧先执行防丢失保存再载入新剧本。
    /// </summary>
    private async void OnScriptSelected(object sender, SelectionChangedEventArgs e)
    {
        if (sender is not ListBox list || list.SelectedItem is not ScriptFileNode node)
        {
            return;
        }
        list.SelectedItem = null;
        await _vm.SwitchToScriptCommand.ExecuteAsync(node);
    }
}
