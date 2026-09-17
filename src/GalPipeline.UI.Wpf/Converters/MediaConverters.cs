using System.Globalization;
using System.Windows.Data;
using GalPipeline.Desktop.ViewModels;
using Wpf.Ui.Controls;

namespace GalPipeline.Desktop.Converters;

/// <summary>
/// 修图状态 → ui:Badge 的外观枚举。
///
/// 为什么要转换而非直接在 VM 暴露枚举：VM 层对 Wpf.Ui 保持**零依赖**
/// （全仓 ViewModels/ 目录 grep 不到 Wpf.Ui 即为契约），
/// 控件外观映射属于视图层职责。
/// </summary>
public sealed class MediaStatusToAppearanceConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        value switch
        {
            MediaAssetStatus.Editing => ControlAppearance.Info,
            MediaAssetStatus.Completed => ControlAppearance.Success,
            _ => ControlAppearance.Secondary,
        };

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        throw new NotSupportedException("修图状态为单向呈现，不支持回写。");
}

/// <summary>
/// 字符串状态筛选 → 是否选中（筛选按钮的视觉态）。
/// </summary>
public sealed class FilterEqualsConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        string.Equals(value?.ToString(), parameter?.ToString(), StringComparison.OrdinalIgnoreCase);

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        throw new NotSupportedException("筛选态由命令驱动，不支持回写。");
}

/// <summary>布尔取反（空态/有值态的互补可见性）。</summary>
public sealed class InverseBooleanConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        value is bool flag && !flag;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture) =>
        value is bool flag && !flag;
}
