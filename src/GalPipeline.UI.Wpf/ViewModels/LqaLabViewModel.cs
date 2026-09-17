using System.Collections.ObjectModel;
using System.Windows;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.Lqa;
using GalPipeline.Desktop.Services;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>看板中的一条违规绑定项（行定位 + 明细 + 原译对比）。</summary>
public sealed partial class LqaViolationItemViewModel : ObservableObject
{
    public LqaViolationItemViewModel(
        LqaViolationItem item, StudioUnitItemViewModel? row, string ruleLabel)
    {
        Item = item;
        Row = row;
        RuleLabel = ruleLabel;
    }

    public LqaViolationItem Item { get; }

    /// <summary>工坊中的对应行（剧本被切换后可能为 null —— 此时只读不可治理）。</summary>
    public StudioUnitItemViewModel? Row { get; }

    public string RuleLabel { get; }

    public string UnitId => Item.UnitId;
    public string LineTag => Item.LineTag;
    public string Speaker => Item.Speaker;
    public string SourceText => Item.SourceText;
    public string TranslatedText => Item.TranslatedText;
    public string Summary => Item.Summary;
    public string RuleId => Item.PrimaryRuleId;

    /// <summary>是否仍可治理（行还在工坊里且未被切换走）。</summary>
    public bool IsActionable => Row is not null;

    /// <summary>违例级别徽章文案：含 error 时显 ERROR，否则 WARNING。</summary>
    public string SeverityBadge =>
        Item.Issues.Any(i => i.Severity == "error") ? "ERROR" : "WARNING";
}

/// <summary>
/// LQA Lab 质检中心视图模型 —— 质量门禁看板 + 违规治理工作台。
///
/// 数据源是工坊单例（<see cref="StudioViewModelProvider.Shared"/>）的实时行数据：
/// 聚合纯函数下沉在 GalPipeline.Core.Lqa.LqaBoard（dotnet test 射程内），
/// 本 VM 只负责「工坊行 → 看板源 → 绑定项」的映射与治理动作路由。
/// 剧本被切换后旧违例行可能已不在工坊 —— IsActionable 如实降级为只读。
/// </summary>
public sealed partial class LqaLabViewModel : ObservableObject
{
    public ObservableCollection<LqaViolationItemViewModel> Violations { get; } = [];

    public ObservableCollection<LqaRuleSummary> RuleSummaries { get; } = [];

    /// <summary>工坊当前总行数（0 = 尚无载入的剧本）。</summary>
    public int TotalUnits => StudioViewModelProvider.Shared.Units.Count;

    /// <summary>工坊当前 LQA_FAILED 行数（治理目标的总盘子）。</summary>
    public int FailedUnits => StudioViewModelProvider.Shared.Units.Count(u => u.IsFailed);

    public bool HasData => TotalUnits > 0;
    public bool HasViolations => Violations.Count > 0;

    public Visibility BoardVisibility =>
        HasData ? Visibility.Visible : Visibility.Collapsed;
    public Visibility EmptyWorkspaceVisibility =>
        HasData ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ViolationsVisibility =>
        HasViolations ? Visibility.Visible : Visibility.Collapsed;
    public Visibility AllGreenVisibility =>
        HasData && !HasViolations ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>从工坊单例重建看板（进入页面 / 手动刷新 / 治理动作完成后调用）。</summary>
    [RelayCommand]
    public void RefreshBoard()
    {
        var shared = StudioViewModelProvider.Shared;
        var snapshot = LqaBoard.Build(shared.Units.Select(u => new LqaViolationSource(
            u.Id, u.LineNumberTag, u.Speaker, u.SourceText, u.TranslatedText, u.GetIssues())));

        Violations.Clear();
        foreach (var item in snapshot.Items)
        {
            var row = shared.Units.FirstOrDefault(u => u.Id == item.UnitId);
            Violations.Add(new LqaViolationItemViewModel(
                item, row, LqaBoard.RuleLabel(item.PrimaryRuleId)));
        }

        RuleSummaries.Clear();
        foreach (var rule in snapshot.Rules)
        {
            RuleSummaries.Add(rule);
        }

        OnPropertyChanged(nameof(TotalUnits));
        OnPropertyChanged(nameof(FailedUnits));
        OnPropertyChanged(nameof(HasData));
        OnPropertyChanged(nameof(HasViolations));
        OnPropertyChanged(nameof(BoardVisibility));
        OnPropertyChanged(nameof(EmptyWorkspaceVisibility));
        OnPropertyChanged(nameof(ViolationsVisibility));
        OnPropertyChanged(nameof(AllGreenVisibility));
    }

    /// <summary>跳转工坊：工坊内定位到该行，并经静态事件请求 MainWindow 导航。</summary>
    [RelayCommand]
    private void FocusViolation(LqaViolationItemViewModel? item)
    {
        if (item?.Row is null)
        {
            return;
        }
        StudioViewModelProvider.Shared.FocusUnit(item.UnitId);
        StudioViewModelProvider.RaiseFocusRequested(item.UnitId);
    }

    /// <summary>单条定向重译：复用工坊的 RetryUnitCommand（含 run_lqa 复检），完成后重建看板。</summary>
    [RelayCommand]
    private async Task RetryViolationAsync(LqaViolationItemViewModel? item)
    {
        if (item?.Row is null)
        {
            return;
        }
        await StudioViewModelProvider.Shared.RetryUnitCommand.ExecuteAsync(item.Row);
        RefreshBoard();
    }
}
