using GalPipeline.Desktop.ViewModels;

namespace GalPipeline.Desktop.Services;

/// <summary>
/// StudioViewModel 的进程级共享入口。
///
/// 为什么必须共享：页面导航按类型 new（每次进 Studio 都是新 StudioPage），
/// 若每个 StudioView 自建 VM，「LQA Lab 看板 / 跳转定位 / 面板内重译」
/// 就永远摸不到工坊的实时行数据。单例化之后：
/// 跨导航保留载入与审校状态；LqaLab 经 <see cref="Shared"/> 直接聚合、
/// 定位、触发重译 —— 全部操作同一份真相。
/// </summary>
public static class StudioViewModelProvider
{
    private static readonly Lazy<StudioViewModel> SharedLazy = new(() => new StudioViewModel());

    /// <summary>进程级唯一的 Studio 工坊视图模型。</summary>
    public static StudioViewModel Shared => SharedLazy.Value;

    /// <summary>LQA Lab 请求跳转到剧本工坊并定位指定行（MainWindow 订阅后导航）。</summary>
    public static event Action<string>? FocusRequested;

    /// <summary>页面请求主壳导航到指定槽位（如资产双击剧本 → Studio）。</summary>
    public static event Action<string>? NavigateRequested;

    internal static void RaiseFocusRequested(string unitId) => FocusRequested?.Invoke(unitId);

    internal static void RaiseNavigateRequested(string tag) => NavigateRequested?.Invoke(tag);
}
