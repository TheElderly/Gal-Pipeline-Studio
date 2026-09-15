using GalPipeline.UI.ViewModels;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.Storage.Pickers;
using Windows.UI;

namespace GalPipeline.UI;

/// <summary>主工作台窗口：三段式布局，x:Bind 函数在本类实现。</summary>
public sealed partial class MainWindow : Window
{
    private static readonly SolidColorBrush ReadyBrush = new(Color.FromArgb(255, 0x46, 0xB3, 0x5B));
    private static readonly SolidColorBrush FaultBrush = new(Color.FromArgb(255, 0xE0, 0x4F, 0x5F));

    public MainViewModel Vm { get; } = new();

    public MainWindow()
    {
        InitializeComponent();
        Title = "Gal-Pipeline Studio";
        SystemBackdrop = new MicaBackdrop
        {
            Kind = Microsoft.UI.Composition.SystemBackdrops.MicaKind.BaseAlt,
        };
        _ = Vm.InitializeAsync();
        Closed += (_, _) => Vm.Dispose();
    }

    // ---------------- 事件处理 ----------------

    /// <summary>文件选择器需要窗口句柄互操作（Unpackaged 桌面端），故置于代码后置。</summary>
    private async void OnBrowseClick(object sender, RoutedEventArgs e)
    {
        var picker = new FileOpenPicker();
        WinRT.Interop.InitializeWithWindow.Initialize(
            picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        picker.FileTypeFilter.Add(".ks");
        picker.FileTypeFilter.Add(".txt");
        var file = await picker.PickSingleFileAsync();
        if (file is null)
        {
            return;
        }
        Vm.CurrentFilePath = file.Path;
        await Vm.LoadFileCommand.ExecuteAsync(file.Path);
    }

    /// <summary>双击解锁译文只读态并聚焦——「直接微调修改」交互。</summary>
    private void OnTranslationDoubleTapped(object sender, DoubleTappedRoutedEventArgs e)
    {
        if (sender is TextBox textBox)
        {
            textBox.IsReadOnly = false;
            textBox.Focus(FocusState.Programmatic);
        }
    }

    /// <summary>失焦即锁定只读并把人工微调回写进 DTO（保证导出取到最新译文）。</summary>
    private void OnTranslationLostFocus(object sender, RoutedEventArgs e)
    {
        if (sender is TextBox textBox)
        {
            textBox.IsReadOnly = true;
            (textBox.DataContext as TranslationUnitItemViewModel)?.CommitEdits();
        }
    }
}
