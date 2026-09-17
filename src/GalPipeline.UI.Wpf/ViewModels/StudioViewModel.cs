using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows.Data;
using System.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using GalPipeline.Core.Audio;
using GalPipeline.Core.Inspector;
using GalPipeline.Core.IPC;
using GalPipeline.Core.Project;
using GalPipeline.Core.Translation;
using GalPipeline.Core.Toolchain;
using GalPipeline.Desktop.Services;
using Microsoft.Win32;

namespace GalPipeline.Desktop.ViewModels;

/// <summary>
/// Studio 剧本工坊视图模型：载入 → 双栏审校（筛选/搜索/批量翻译/LQA 门禁/
/// 人工微调/单条重试）→ 导出回写；右栏情境审查抽屉。
/// 首屏由 <see cref="InitializeAsync"/> 经真实 RPC 载入内置 KAG 样本
/// （早期版本在此注入硬编码演示数据，已废除）。
/// </summary>
public sealed partial class StudioViewModel : ObservableObject, IDisposable
{
    /// <summary>历史常量：本地环回 Mock 端点（现以
    /// <see cref="TranslationSettingsStore.DefaultMockApiBase"/> 为唯一真源，此别名保持兼容）。</summary>
    public const string DefaultMockApiBase = TranslationSettingsStore.DefaultMockApiBase;

    private GalIRProjectDto? _currentProject;

    /// <summary>音频回放服务：WPF MediaPlayer 实现，解码在媒体管道异步完成。</summary>
    private readonly IAudioPlayer _audioPlayer = new WpfAudioPlayer();

    /// <summary>
    /// FFmpeg 转码缓存：OGG 等非 WMSDK 容器经此归一为 WAV 后再交给播放器
    /// （Galgame 语音 90% 是 OGG —— FFmpeg 是多媒体唯一底层，不做任何
    /// 「请自行转码」的回避）。
    /// </summary>
    private readonly FFmpegAudioCache _audioCache = new();

    /// <summary>在途转码任务的取消源：快速切行时立即终止上一个 FFmpeg 子进程。</summary>
    private CancellationTokenSource? _transcodeCts;

    /// <summary>当前会话是否处于暂停态（区别于「无会话」）。</summary>
    private bool _audioPaused;

    /// <summary>
    /// 统计抑制闸：批量回填 / 整表替换期间挂起逐行触发的进度重算。
    /// 不挂起的话，回填 N 行会触发 N 次全表 O(n) 聚合（O(n²)）；
    /// 大剧本（3500 行）下这一项就足以卡住 UI 线程。
    /// </summary>
    private bool _suspendStats;

    /// <summary>指标通报口：MainWindow 注入后，Studio 的（文件标签 / 指标 / 状态文案）
    /// 实时汇入主窗底栏与内容区说明条。</summary>
    public static Action<string, string, string>? MetricsSink;

    /// <summary>
    /// 筛选复位信号：载入新文件时通知视图把筛选胶囊的选中态归位。
    /// 胶囊由 Command + CommandParameter 驱动（非 IsChecked 双向绑定），
    /// 故 VM 侧改 FilterMode 不会自动反映到 RadioButton 视觉态。
    /// </summary>
    public event Action? FiltersReset;

    public ObservableCollection<StudioUnitItemViewModel> Units { get; } = [];

    public ICollectionView UnitsView { get; }

    [ObservableProperty]
    public partial string CurrentFilePath { get; set; } = string.Empty;

    // ------------------------------------------------------------------
    // P1 工程树：整作目录（游戏根）→ 引擎画像 → .galpipeline 工作区 → 剧本列表
    // ------------------------------------------------------------------

    /// <summary>当前打开的游戏根目录（整作工程模式；单文件模式为空串）。</summary>
    [ObservableProperty]
    public partial string GameDirectory { get; set; } = string.Empty;

    /// <summary>引擎画像：判定类型（未知引擎为 null）。</summary>
    [ObservableProperty]
    public partial string? CurrentEngineType { get; set; }

    /// <summary>引擎画像：界面可读名（如 KiriKiri / KAG（吉里吉里））。</summary>
    [ObservableProperty]
    public partial string? CurrentEngineDisplay { get; set; }

    /// <summary>引擎画像：判定置信度 ∈ [0,1]。</summary>
    [ObservableProperty]
    public partial double EngineConfidence { get; set; }

    /// <summary>引擎画像：命中特征摘要（供徽章 Tooltip 逐条展开）。</summary>
    [ObservableProperty]
    public partial string EngineEvidenceSummary { get; set; } = string.Empty;

    /// <summary>工程树数据源：当前游戏目录下的全部剧本（自然序）。</summary>
    public System.Collections.ObjectModel.ObservableCollection<ScriptFileNode> ProjectScripts { get; } = [];

    public bool HasProjectScripts => ProjectScripts.Count > 0;

    public System.Windows.Visibility ProjectExplorerVisibility =>
        HasProjectScripts ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public bool HasEngineBadge => CurrentEngineType is not null;

    public System.Windows.Visibility EngineBadgeVisibility =>
        HasEngineBadge ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    /// <summary>引擎识别徽章：`KiriKiri / KAG（吉里吉里）· 置信度 90%`。</summary>
    public string EngineBadgeText => CurrentEngineType is null
        ? string.Empty
        : $"{CurrentEngineDisplay} · 置信度 {Math.Round(EngineConfidence * 100).ToString(System.Globalization.CultureInfo.InvariantCulture)}%";

    /// <summary>切换剧本时的防丢失回执（随载入成功文案一并呈现，一次性）。</summary>
    private string? _pendingSaveNote;

    // ------------------------------------------------------------------
    // 一键解包（模块 1 闭环）：识别到引擎但无裸剧本 → 引导 GARbro CLI 解包
    // ------------------------------------------------------------------

    // 解析器由 ToolchainRuntime 动态供给：设置页改自定义路径后即刻生效
    // （严禁回退为 new ExternalToolResolver() —— 那会复活「改了不生效」）
    private static IToolResolver ToolResolver => ToolchainRuntime.Current;

    /// <summary>解包批次的取消源：取消后停止派发后续封包（无失控长任务）。</summary>
    private CancellationTokenSource? _unpackCts;

    /// <summary>
    /// 解包引导态：识别到已知引擎**且**未扫出裸剧本（判定收敛到共享层
    /// <see cref="ScriptWorkspace.NeedsUnpack"/>）—— 典型的纯封包盘形态。
    /// </summary>
    [ObservableProperty]
    public partial bool NeedsUnpackPrompt { get; set; }

    [ObservableProperty]
    public partial bool IsUnpacking { get; set; }

    /// <summary>解包进度 ∈ [0,1]（已处理封包 / 总数 —— 真实进度，不做假动画）。</summary>
    [ObservableProperty]
    public partial double UnpackProgress { get; set; }

    public System.Windows.Visibility UnpackPromptVisibility =>
        NeedsUnpackPrompt ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility UnpackWorkingVisibility =>
        IsUnpacking ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility ProjectPanelVisibility =>
        HasProjectScripts || NeedsUnpackPrompt
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public bool CanStartUnpack => NeedsUnpackPrompt && !IsUnpacking && !IsBusy;

    partial void OnNeedsUnpackPromptChanged(bool value)
    {
        OnPropertyChanged(nameof(UnpackPromptVisibility));
        OnPropertyChanged(nameof(ProjectPanelVisibility));
        OnPropertyChanged(nameof(CanStartUnpack));
    }

    partial void OnIsUnpackingChanged(bool value)
    {
        OnPropertyChanged(nameof(UnpackWorkingVisibility));
        OnPropertyChanged(nameof(CanStartUnpack));
    }

    // 端点 / 模型 / 温度 / 推理力度等翻译配置已统一收敛到
    // TranslationSettingsStore（Settings 页写入）—— 此前这里另持一份
    // TranslateApiBase / ModelName 且从不与设置页互通，正是「改了不生效」
    // 的根因，两个属性已彻底删除。

    [ObservableProperty]
    public partial bool IsBusy { get; set; }

    [ObservableProperty]
    public partial string StatusMessage { get; set; } = "就绪。";

    [ObservableProperty]
    public partial int PassedCount { get; set; }

    [ObservableProperty]
    public partial int FailedCount { get; set; }

    [ObservableProperty]
    public partial int TotalCount { get; set; }

    /// <summary>已有译文的行数（**不看质检结论**）—— 进度条的分子。</summary>
    [ObservableProperty]
    public partial int TranslatedCount { get; set; }

    /// <summary>待翻译行数：EXTRACTED 且译文空白（批量按钮角标与选取目标的唯一数据源）。</summary>
    [ObservableProperty]
    public partial int PendingTranslationCount { get; set; }

    /// <summary>
    /// 批量翻译在途标记：既是防重入闸门，也驱动按钮可用性。
    /// 与 <see cref="IsBusy"/> 刻意分开 —— IsBusy 是全局忙（载入 / 导出 / 翻译共用），
    /// 本标记只描述「批量翻译」这一件事，便于给按钮挂独立的加载态而不牵连整页。
    /// </summary>
    [ObservableProperty]
    public partial bool IsTranslating { get; set; }

    /// <summary>
    /// 单次 RPC 携带的单元数（客户端切片粒度 = 并发调度粒度）。
    /// 切片承载**进度分档**；切片内部由 Sidecar 按 config.batch_size（默认 8）
    /// 二次切分并以并发池（concurrency_limit，默认 3）同时发出 ——
    /// 24 = 8 × 3，让进度推进与吞吐提升同时成立。
    /// </summary>
    [ObservableProperty]
    public partial int TranslateChunkSize { get; set; } = 24;

    [ObservableProperty]
    public partial string SearchText { get; set; } = string.Empty;

    [ObservableProperty]
    public partial string FilterMode { get; set; } = "All";

    [ObservableProperty]
    public partial StudioUnitItemViewModel? SelectedUnit { get; set; }

    [ObservableProperty]
    public partial bool IsInspectorOpen { get; set; } = true;

    /// <summary>
    /// 试听可用性：本行有语音线索 **且** 解析后的音频文件真实存在。
    /// 与行级 <c>IsPlaybackEnabled</c>（只看元数据）刻意分层 —— 文件缺失
    /// 时按钮必须置灰并给出缺失路径提示，可点却无声比灰掉更糟。
    /// </summary>
    [ObservableProperty]
    public partial bool IsAudioPlayable { get; set; }

    /// <summary>播放键 Tooltip：可播时显示曲目，不可播时给出精确原因。</summary>
    [ObservableProperty]
    public partial string AudioStatusHint { get; set; } = "选中一行后可试听其配音。";

    /// <summary>是否有活跃试听会话（播放中或暂停）。</summary>
    [ObservableProperty]
    public partial bool IsAudioSessionActive { get; set; }

    /// <summary>导出目录（恒为绝对路径，由 <see cref="ResolveExportDirectory"/> 解析：源文件同级 localized/）。</summary>
    [ObservableProperty]
    public partial string ExportDirectory { get; set; } = string.Empty;

    /// <summary>最近一次导出产物的绝对路径（导出成功后由底栏回显；未导出为空串）。</summary>
    [ObservableProperty]
    public partial string LastExportPath { get; set; } = string.Empty;

    /// <summary>
    /// 最近一次**实测**往返耗时（毫秒）；<c>null</c> 表示尚未测量。
    ///
    /// 刻意用可空而非预填一个好看的数字：底栏在未测量时显示 <c>Latency: —</c>
    /// （诚实空态），只有真的跑过一次 RPC 才会出现读数。
    /// 此前该属性初值是写死的 <c>"Latency: 180ms"</c>，属于凭空捏造的仪表读数。
    /// </summary>
    [ObservableProperty]
    public partial double? LastLatencyMs { get; set; }

    /// <summary>底栏延迟段：未测量时诚实占位，绝不预填数字。</summary>
    public string LatencyText => LastLatencyMs is { } ms
        ? $"Latency: {ms.ToString("N0", System.Globalization.CultureInfo.InvariantCulture)}ms"
        : "Latency: —";

    public bool HasSelectedUnit => SelectedUnit is not null;

    /// <summary>
    /// 抽屉标题：选行联动（<c>Inspector #00142</c> / 未选中时为 <c>Context Inspector</c>）。
    /// 格式化落在共享层 <see cref="InspectorState.TitleFor"/>，与行 VM 的行号标签同源。
    /// </summary>
    public string InspectorTitle =>
        SelectedUnit?.InspectorTitle ?? InspectorState.NoSelectionTitle;

    /// <summary>抽屉宽度：展开 280 / 折叠 32（停靠条，常驻再展开把手）。</summary>
    public double InspectorWidth => IsInspectorOpen ? 280 : 32;

    public System.Windows.Visibility InspectorContentVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorCollapseVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorExpandVisibility =>
        IsInspectorOpen ? System.Windows.Visibility.Collapsed : System.Windows.Visibility.Visible;

    public System.Windows.Visibility InspectorEmptyVisibility =>
        IsInspectorOpen && SelectedUnit is null
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public System.Windows.Visibility InspectorSelectedVisibility =>
        IsInspectorOpen && SelectedUnit is not null
            ? System.Windows.Visibility.Visible
            : System.Windows.Visibility.Collapsed;

    public bool IsIdle => !IsBusy;

    /// <summary>最近一次整表统计快照（仅在通知时重算一次，避免 getter 反复 O(n) 聚合）。</summary>
    private TranslationProgressSnapshot _snapshot;

    /// <summary>
    /// 译文覆盖率 ∈ [0, 1]，直接喂 <c>ProgressBar.Value</c>（配 <c>Maximum="1"</c>）。
    /// 公式落在共享库 <see cref="TranslationProgressSnapshot.Ratio"/>，与自动化测试同源。
    /// </summary>
    public double TranslationProgressRatio => _snapshot.Ratio;

    /// <summary>进度文案，如 <c>67% Translated</c>（设计稿口径）。</summary>
    public string TranslationProgressText => _snapshot.Text;

    /// <summary>
    /// 批量翻译可用性：无在途任务、无全局忙、且确有可翻的行。
    /// 待译数为 0 时按钮置灰 —— 既避免空转 RPC，也让角标 "(0)" 成为明确的「已翻完」信号。
    /// </summary>
    public bool CanTranslateBatch => !IsTranslating && !IsBusy && PendingTranslationCount > 0;

    /// <summary>批量按钮角标：只数「待翻译」的行（终态与失败行都不算，见 <c>NeedsTranslation</c>）。</summary>
    public string BatchTranslateLabel => $"Batch Translate ({PendingTranslationCount})";

    public string ProgressLabel =>
        $"已通过 {PassedCount} / 待翻译 {PendingTranslationCount} / 不合格 {FailedCount}，共 {TotalCount} 条";

    public string FileStatusLabel =>
        string.IsNullOrWhiteSpace(CurrentFilePath)
            ? "未载入文件"
            : $"{Path.GetFileName(CurrentFilePath)} · {TotalCount} 行";

    partial void OnIsBusyChanged(bool value)
    {
        OnPropertyChanged(nameof(IsIdle));
        OnPropertyChanged(nameof(CanTranslateBatch));
        OnPropertyChanged(nameof(CanStartUnpack));
    }

    partial void OnStatusMessageChanged(string value) => PublishMetrics();

    partial void OnLastLatencyMsChanged(double? value)
    {
        OnPropertyChanged(nameof(LatencyText));
        PublishMetrics();
    }

    partial void OnLastExportPathChanged(string value) => PublishMetrics();

    partial void OnTotalCountChanged(int value) => OnPropertyChanged(nameof(FileStatusLabel));

    partial void OnIsTranslatingChanged(bool value) => OnPropertyChanged(nameof(CanTranslateBatch));

    partial void OnPendingTranslationCountChanged(int value) =>
        OnPropertyChanged(nameof(CanTranslateBatch));

    partial void OnSearchTextChanged(string value) => UnitsView.Refresh();
    partial void OnFilterModeChanged(string value) => UnitsView.Refresh();
    partial void OnCurrentFilePathChanged(string value)
    {
        OnPropertyChanged(nameof(FileStatusLabel));
        RefreshAudioState();
    }

    partial void OnCurrentEngineTypeChanged(string? value)
    {
        OnPropertyChanged(nameof(HasEngineBadge));
        OnPropertyChanged(nameof(EngineBadgeVisibility));
        OnPropertyChanged(nameof(EngineBadgeText));
    }

    partial void OnCurrentEngineDisplayChanged(string? value) => OnPropertyChanged(nameof(EngineBadgeText));

    partial void OnEngineConfidenceChanged(double value) => OnPropertyChanged(nameof(EngineBadgeText));
    partial void OnIsInspectorOpenChanged(bool value)
    {
        OnPropertyChanged(nameof(InspectorWidth));
        OnPropertyChanged(nameof(InspectorContentVisibility));
        OnPropertyChanged(nameof(InspectorCollapseVisibility));
        OnPropertyChanged(nameof(InspectorExpandVisibility));
        OnPropertyChanged(nameof(InspectorEmptyVisibility));
        OnPropertyChanged(nameof(InspectorSelectedVisibility));
    }
    partial void OnSelectedUnitChanged(StudioUnitItemViewModel? value)
    {
        OnPropertyChanged(nameof(HasSelectedUnit));
        OnPropertyChanged(nameof(InspectorEmptyVisibility));
        OnPropertyChanged(nameof(InspectorSelectedVisibility));
        OnPropertyChanged(nameof(InspectorTitle));
        // 波形 / 术语 / 音频态的派生已全部下移到行 VM：选中行只是一次引用切换，
        // Inspector 各字段经 SelectedUnit.X 直接读到该行的快照，父 VM 不再持有副本，
        // 也就不存在「父副本 vs 行数据」两处不一致的可能。
        RefreshAudioState();
    }

    /// <summary>抽屉折叠开关。</summary>
    [RelayCommand]
    private void ToggleInspector() => IsInspectorOpen = !IsInspectorOpen;

    // ------------------------------------------------------------------
    // 试听（Inspector 音频卡）：此前播放三键是无 Command 的死控件 ——
    // 可点却无声比灰掉更糟。现在三键各接真实命令：前后导航切行，
    // 中键按「有会话/暂停/无会话」三态驱动 IAudioPlayer。
    // ------------------------------------------------------------------

    /// <summary>选中上一行（列表头则保持不动）。</summary>
    [RelayCommand]
    private void PreviousUnit() => MoveSelection(-1);

    /// <summary>选中下一行（列表尾则保持不动）。</summary>
    [RelayCommand]
    private void NextUnit() => MoveSelection(1);

    private void MoveSelection(int delta)
    {
        if (Units.Count == 0)
        {
            return;
        }
        var index = SelectedUnit is null ? 0 : Units.IndexOf(SelectedUnit);
        if (index < 0)
        {
            return;
        }
        SelectedUnit = Units[Math.Clamp(index + delta, 0, Units.Count - 1)];
    }

    /// <summary>
    /// LQA Lab「定位到工坊」入口：清筛选/搜索（否则目标行可能被滤掉，
    /// 选中赋值会被视图层静默忽略），再把选中态指向目标行。
    /// 导航由 MainWindow 经 FocusRequested 事件承载。
    /// </summary>
    public void FocusUnit(string unitId)
    {
        SearchText = string.Empty;
        FilterMode = "All";
        FiltersReset?.Invoke();
        UnitsView.Refresh();
        SelectedUnit = Units.FirstOrDefault(u => u.Id == unitId)
            ?? SelectedUnit ?? Units.FirstOrDefault();
    }

    /// <summary>播放/暂停三态切换：播放中 → 暂停；暂停中 → 续播；空闲 → 开新会话。</summary>
    [RelayCommand]
    private async Task PlayPauseAudio()
    {
        if (SelectedUnit is null || !IsAudioPlayable)
        {
            return;
        }
        if (_audioPaused)
        {
            _audioPlayer.Resume();
            _audioPaused = false;
            StatusMessage = $"继续试听 {Path.GetFileName(ResolveAudioPath(SelectedUnit))}。";
            return;
        }
        if (IsAudioSessionActive)
        {
            _audioPlayer.Pause();
            _audioPaused = true;
            StatusMessage = "试听已暂停。";
            return;
        }
        var source = ResolveAudioPath(SelectedUnit);
        if (source is null)
        {
            return;
        }
        CancelPendingTranscode();
        var session = _transcodeCts = new CancellationTokenSource();
        StatusMessage = $"正在解码 {Path.GetFileName(source)}…";
        try
        {
            var outcome = await _audioCache.GetPlayableAsync(source, session.Token);
            if (session.IsCancellationRequested)
            {
                return; // 行已切走：丢弃结果，绝不把上一行的音频播进当前会话
            }
            if (outcome.PlayablePath is null)
            {
                StatusMessage = outcome.FailureReason ?? "音频不可播。";
                return;
            }
            _audioPlayer.Play(outcome.PlayablePath);
            _audioPaused = false;
            IsAudioSessionActive = true;
            StatusMessage = $"试听 {Path.GetFileName(source)}（经 FFmpeg 管道归一后回放）。";
        }
        catch (OperationCanceledException)
        {
            // 快速切行打断在途转码：预期路径，静默（切行侧已给出新状态）
        }
        catch (Exception ex)
        {
            StatusMessage = $"音频管道失败：{ex.Message}";
        }
    }

    /// <summary>
    /// 终止在途转码：FFmpeg 子进程被 Kill(entireProcessTree)，
    /// 快速连续切行（按住方向键）不产生僵尸进程、不泄漏句柄。
    /// </summary>
    private void CancelPendingTranscode()
    {
        if (_transcodeCts is { } cts)
        {
            _transcodeCts = null;
            try
            {
                cts.Cancel();
            }
            finally
            {
                cts.Dispose();
            }
        }
    }

    /// <summary>
    /// 解析选中行的音频绝对路径：相对路径以**当前剧本所在目录**为基
    /// （与源文件同级的 voice/ 等约定），无源文件时退回仓库根。
    /// 返回 null 表示该行有语音线索但未携带资源名。
    /// </summary>
    private string? ResolveAudioPath(StudioUnitItemViewModel unit)
    {
        var file = AudioCue.FromMetadata(unit.Model.Metadata)?.File;
        if (string.IsNullOrWhiteSpace(file))
        {
            return null;
        }
        if (Path.IsPathRooted(file))
        {
            return file;
        }
        var baseDir = string.IsNullOrWhiteSpace(CurrentFilePath)
            ? LocateRepositoryRoot()
            : Path.GetDirectoryName(Path.GetFullPath(CurrentFilePath));
        return baseDir is null ? file : Path.GetFullPath(Path.Combine(baseDir, file));
    }

    /// <summary>切行 / 换文件后重算试听可用性与提示文案（置灰必须给出精确原因）。</summary>
    private void RefreshAudioState()
    {
        CancelPendingTranscode(); // 快速切行：立即终止上一行未完成的 FFmpeg 转码
        _audioPlayer.Stop();
        _audioPaused = false;
        IsAudioSessionActive = false;

        var unit = SelectedUnit;
        if (unit is null || !unit.HasVoice)
        {
            IsAudioPlayable = false;
            AudioStatusHint = unit is null
                ? "选中一行后可试听其配音。"
                : "该行没有语音线索（metadata.audio / metadata.voice）。";
            return;
        }
        var file = ResolveAudioPath(unit);
        if (file is null)
        {
            IsAudioPlayable = false;
            AudioStatusHint = "该行携带语音时长/补偿信息，但未指明音频资源名。";
            return;
        }
        if (!File.Exists(file))
        {
            IsAudioPlayable = false;
            AudioStatusHint = $"音频文件缺失：{file}";
            return;
        }
        IsAudioPlayable = true;
        AudioStatusHint = $"试听 {Path.GetFileName(file)}";
    }


    public StudioViewModel()
    {
        UnitsView = CollectionViewSource.GetDefaultView(Units);
        UnitsView.Filter = FilterUnit;
        ExportDirectory = ResolveExportDirectory();
        // 播放失败与自然播完都回到统一状态；MediaPlayer 是 DispatcherObject，
        // 事件在 UI 线程回调，直接写 ObservableProperty 安全。
        _audioPlayer.PlaybackFailed += (_, reason) =>
        {
            IsAudioSessionActive = false;
            StatusMessage = reason;
        };
        _audioPlayer.PlaybackEnded += (_, _) => IsAudioSessionActive = false;
    }

    /// <summary>
    /// 启动就绪后的自动载入：走**完整真实 RPC 链路**（detect → extract）把内置
    /// KAG 样本灌入审校网格。取代早期的硬编码演示行 —— 首屏依旧饱满，但宏药丸
    /// 展示的是引擎原生标记字面量（<c>[r]</c> / <c>[ruby text="…"]</c> / <c>[p]</c>），
    /// 与回写时的逐字节复写对象完全一致。
    /// 样本缺失时降级为空态 + 提示，不抛异常、不崩启动。
    /// </summary>
    private bool _initialized;

    public async Task InitializeAsync()
    {
        // VM 已单例化（跨导航共享）：样本只自动载一次，反复导航不重拉
        if (_initialized)
        {
            return;
        }
        _initialized = true;
        var sample = LocateSampleScript();
        if (sample is null)
        {
            StatusMessage = "未找到内置样本剧本，请拖入文件或经「打开剧本…」载入。";
            return;
        }
        await LoadFileAsync(sample);
    }

    /// <summary>内置真实样本剧本（真实 KAG .ks：注音宏 / 行内控制符 / 跳转标签 / 旁白）。
    /// 取代早期的硬编码演示行 —— 首屏依旧饱满，但每一行都来自适配器真实解析结果。</summary>
    private static string? LocateSampleScript()
    {
        var root = LocateRepositoryRoot();
        if (root is null)
        {
            return null;
        }
        var candidate = Path.Combine(root, "tests", "fixtures", "sample_act1.ks");
        return File.Exists(candidate) ? candidate : null;
    }

    /// <summary>定位仓库根：以 core/server/rpc.py 为锚点向上回溯（与 IPC 客户端同源锚点）。</summary>
    private static string? LocateRepositoryRoot()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "core", "server", "rpc.py")))
            {
                return directory.FullName;
            }
            directory = directory.Parent;
        }
        return null;
    }

    /// <summary>
    /// 导出目录的统一解析（恒返回绝对路径）：源文件同级的 localized/。
    /// 与源剧本同目录便于原地比对，且不污染仓库根；尚无源文件时退化为
    /// 仓库根下 localized/。壳层不再持有相对路径，杜绝 CWD 漂移导致写歪。
    /// </summary>
    public string ResolveExportDirectory()
    {
        if (!string.IsNullOrWhiteSpace(CurrentFilePath))
        {
            try
            {
                var sourceDir = Path.GetDirectoryName(Path.GetFullPath(CurrentFilePath));
                if (!string.IsNullOrWhiteSpace(sourceDir))
                {
                    return Path.Combine(sourceDir, "localized");
                }
            }
            catch (ArgumentException)
            {
                // 路径非法（含非法字符等）：退化为仓库根，绝不让解析异常冒泡到 UI
            }
        }
        var root = LocateRepositoryRoot() ?? AppContext.BaseDirectory;
        return Path.Combine(root, "localized");
    }

    private bool FilterUnit(object item)
    {
        if (item is not StudioUnitItemViewModel unit)
        {
            return false;
        }
        var pass = FilterMode switch
        {
            "Pending" => unit.IsPending,
            "Failed" => unit.IsFailed,
            _ => true,
        };
        if (!pass)
        {
            return false;
        }
        if (string.IsNullOrWhiteSpace(SearchText))
        {
            return true;
        }
        return unit.SourceText.Contains(SearchText, StringComparison.OrdinalIgnoreCase)
               || (unit.TranslatedText?.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ?? false)
               || unit.Speaker.Contains(SearchText, StringComparison.OrdinalIgnoreCase)
               || unit.Id.Contains(SearchText, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>筛选器胶囊：All / Pending / Failed。</summary>
    [RelayCommand]
    private void SetFilter(string? mode) => FilterMode = string.IsNullOrWhiteSpace(mode) ? "All" : mode;

    /// <summary>打开文件选择器（*.ks / *.txt），选定后走统一载入链。</summary>
    [RelayCommand]
    private async Task OpenFilePickerAsync()
    {
        var dialog = new OpenFileDialog
        {
            Filter = "Galgame Script (*.ks;*.txt)|*.ks;*.txt|All files (*.*)|*.*",
            Title = "选择剧本文件",
        };
        if (dialog.ShowDialog() == true)
        {
            await LoadFileAsync(dialog.FileName);
        }
    }

    /// <summary>打开游戏根目录（P1 整作工程模式入口；拖入目录同路）。</summary>
    [RelayCommand]
    private async Task OpenDirectoryPickerAsync()
    {
        var dialog = new OpenFolderDialog { Title = "选择游戏根目录" };
        if (dialog.ShowDialog() == true)
        {
            await LoadGameDirectoryAsync(dialog.FolderName);
        }
    }

    /// <summary>
    /// 载入整作游戏目录：引擎画像（模块 0）→ .galpipeline 工作区（模块 1）
    /// → 剧本树 →（若当前无载入文件）自动载入首个剧本。
    /// </summary>
    [RelayCommand]
    public async Task LoadGameDirectoryAsync(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !Directory.Exists(path))
        {
            StatusMessage = "目录不存在，请检查路径。";
            return;
        }
        await RunBusyAsync(() => LoadGameDirectoryCoreAsync(path));
    }

    private async Task LoadGameDirectoryCoreAsync(string dir)
    {
        var client = SidecarClientProvider.Client;
        var profile = await client.DetectEngineAsync(dir);

        GameDirectory = dir;
        CurrentEngineType = profile.Detected ? profile.EngineType : null;
        CurrentEngineDisplay = profile.EngineDisplay;
        EngineConfidence = profile.Confidence;
        EngineEvidenceSummary = profile.Evidence.Count == 0
            ? "无特征命中"
            : string.Join(" + ", profile.Evidence.Select(e => e.Description));

        // 已识别引擎 → 自动初始化标准工作区（幂等：不覆盖既有 project.json）
        if (profile.Detected)
        {
            await client.InitWorkspaceAsync(dir);
        }

        RefreshProjectScripts(dir, profile.EngineType);

        if (profile.Detected)
        {
            StatusMessage =
                $"已识别引擎 {profile.EngineDisplay}（置信度 {Math.Round(profile.Confidence * 100).ToString(System.Globalization.CultureInfo.InvariantCulture)}%）"
                + $"，命中：{EngineEvidenceSummary}。已初始化 .galpipeline 工作区。";
        }
        else
        {
            // 未知引擎：诚恳引导，不强行中断 —— 单文件审校链路照常可用
            StatusMessage = "未能识别已知引擎 —— 已照常打开目录。若剧本在封包内，"
                + "请先用外部解包工具（如 GARbro）取出脚本文件后再拖入单个 .ks 继续。";
        }

        // 解包引导：识别到引擎但无裸剧本（纯封包盘）→ 一键解包提示
        NeedsUnpackPrompt = ScriptWorkspace.NeedsUnpack(
            profile.Detected, ProjectScripts.Count);

        // 首次进入工程模式：自动载入第一个剧本；已打开文件则保持，由用户从树中切换
        if (Units.Count == 0 && ProjectScripts.Count > 0)
        {
            await LoadFileCoreAsync(ProjectScripts[0].FullPath);
        }
        else if (ProjectScripts.Count == 0 && !NeedsUnpackPrompt)
        {
            StatusMessage += " 未在该目录发现可扫描的剧本文件。";
        }
    }

    /// <summary>按引擎配方重扫工程树（目录载入与解包完成后的共用刷新点）。</summary>
    [RelayCommand]
    private void RefreshProjectTree()
    {
        if (GameDirectory is { } dir)
        {
            RefreshProjectScripts(dir, CurrentEngineType);
        }
    }

    private void RefreshProjectScripts(string gameDir, string? engineType)
    {
        ProjectScripts.Clear();
        foreach (var entry in ScriptWorkspace.ScanScripts(gameDir, engineType))
        {
            ProjectScripts.Add(new ScriptFileNode(entry.FullPath, entry.RelativePath, entry.SizeBytes));
        }
        OnPropertyChanged(nameof(HasProjectScripts));
        OnPropertyChanged(nameof(ProjectExplorerVisibility));
        OnPropertyChanged(nameof(ProjectPanelVisibility));
    }

    /// <summary>
    /// 一键解包：统一工具链解析 GARbro → 列封包清单 → **逐封包短 RPC** 解包到
    /// raw/scripts/ → 重扫工程树（即刻亮起）→（必要时）自动载入首个剧本。
    ///
    /// 逐封包调度的理由：解包可能耗时数分钟，单条长 RPC 会堵死 StdIO 分发器
    /// 且无法取消 —— 拆开后取消粒度 = 封包（批间检查取消令牌，取消后不再派发
    /// 后续封包），进度 = 已处理/总数，单封包失败跳过续走。工具 Missing 时不
    /// 静默降级：抛 ToolchainMissingException，由 RunBusyAsync 把装配引导送达状态栏。
    /// </summary>
    [RelayCommand]
    private async Task StartUnpackAsync()
    {
        if (IsUnpacking || !NeedsUnpackPrompt
            || string.IsNullOrWhiteSpace(GameDirectory) || CurrentEngineType is null)
        {
            return;
        }
        await RunBusyAsync(async () =>
        {
            var info = ToolchainRegistry.Get(ToolType.GARbro);
            var status = ToolResolver.Resolve(ToolType.GARbro);
            if (status.Kind is ToolStatusKind.Missing)
            {
                throw new ToolchainMissingException(info, status.Detail);
            }
            if (!status.IsReady)
            {
                throw new ToolchainException(
                    $"解包工具未通过健康检查（{status.Kind}）：{status.Detail}");
            }

            var client = SidecarClientProvider.Client;
            var archives = await client.ListArchivesAsync(GameDirectory, CurrentEngineType);
            if (archives.Archives.Count == 0)
            {
                NeedsUnpackPrompt = false;
                StatusMessage = "目录中未发现可解包的封包资产。";
                return;
            }

            IsUnpacking = true;
            UnpackProgress = 0;
            _unpackCts = new CancellationTokenSource();
            var session = _unpackCts;
            var processed = 0;
            var failed = 0;
            try
            {
                foreach (var archive in archives.Archives)
                {
                    session.Token.ThrowIfCancellationRequested();
                    try
                    {
                        await client.UnpackArchiveAsync(
                            GameDirectory, CurrentEngineType, archive, status.Path!,
                            session.Token);
                    }
                    catch (JsonRpcException ex)
                    {
                        // 单封包失败不拖垮整批：跳过并上报，继续下一封包
                        failed++;
                        StatusMessage = $"解包 {archive} 失败 [{ex.Code}]：{ex.Message}";
                    }
                    processed++;
                    UnpackProgress = (double)processed / archives.Archives.Count;
                }
            }
            catch (OperationCanceledException)
            {
                // 用户取消：停止派发后续封包（当前子进程受 Python 侧超时治理，无失控长任务）
                RefreshProjectScripts(GameDirectory, CurrentEngineType);
                NeedsUnpackPrompt = ScriptWorkspace.NeedsUnpack(
                    CurrentEngineType is not null, ProjectScripts.Count);
                StatusMessage = $"已取消解包（完成 {processed - failed}/{archives.Archives.Count} 个封包）。";
                return;
            }
            finally
            {
                IsUnpacking = false;
            }

            // 解包完成：重扫工程树即刻亮起；空表格时自动载入首个剧本
            RefreshProjectScripts(GameDirectory, CurrentEngineType);
            if (ProjectScripts.Count > 0)
            {
                NeedsUnpackPrompt = false;
                if (Units.Count == 0)
                {
                    await LoadFileCoreAsync(ProjectScripts[0].FullPath);
                }
                StatusMessage =
                    $"解包完成：{processed}/{archives.Archives.Count} 个封包"
                    + (failed > 0 ? $"（失败 {failed} 个，详见状态栏）" : "")
                    + $"，发现 {ProjectScripts.Count} 个剧本。";
            }
            else
            {
                StatusMessage =
                    $"解包完成：{processed}/{archives.Archives.Count} 个封包，"
                    + "但未在产物中发现可扫描的剧本 —— 该引擎的脚本抽取适配器尚未实现。";
            }
        });
    }

    /// <summary>取消解包批次：停止派发后续封包。</summary>
    [RelayCommand]
    private void CancelUnpack()
    {
        _unpackCts?.Cancel();
        StatusMessage = "正在取消解包…";
    }

    /// <summary>
    /// 工程树点击切换剧本（P1 防丢失闭环）：**先**提交编辑缓冲并对有译文的
    /// 工程自动落盘 localized/，**再**载入新剧本 —— 顺序反了就是静默丢稿。
    /// </summary>
    [RelayCommand]
    private async Task SwitchToScriptAsync(ScriptFileNode? node)
    {
        if (node is null
            || string.Equals(node.FullPath, CurrentFilePath, StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        if (IsBusy)
        {
            StatusMessage = "当前有任务在途，请稍后再切换剧本。";
            return;
        }
        await RunBusyAsync(async () =>
        {
            await SavePendingEditsAsync();
            await LoadFileCoreAsync(node.FullPath);
        });
    }

    /// <summary>
    /// 切换前的防丢失保存：全量 <c>CommitEdits</c> 同步编辑缓冲；
    /// 只要存在任何已填写译文（判定收敛到共享层
    /// <see cref="ScriptWorkspace.HasUnsavedTranslations"/>，半成品也算），
    /// 就自动经 ir_to_asset 覆盖写 localized/ —— 幂等、不碰原文件，
    /// 宁可多存一次也绝不静默丢掉用户敲的字。
    /// </summary>
    private async Task SavePendingEditsAsync()
    {
        if (_currentProject is null || Units.Count == 0)
        {
            return;
        }
        foreach (var unit in Units)
        {
            unit.CommitEdits();
        }
        if (!ScriptWorkspace.HasUnsavedTranslations(
                Units.Select(u => ((string?)u.Status, (string?)u.TranslatedText))))
        {
            return;
        }
        var savedFile = Path.GetFileName(CurrentFilePath);
        var result = await ExportProjectCoreAsync();
        _pendingSaveNote =
            $"已自动保存 {savedFile} 的审校修改 → {Path.GetFileName(Path.GetDirectoryName(result.OutputPath))}/。";
    }

    /// <summary>载入剧本（公开命令，支持文件选择器与拖放复用）。</summary>
    [RelayCommand]
    public async Task LoadFileAsync(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            StatusMessage = "文件不存在，请检查路径。";
            return;
        }
        await RunBusyAsync(() => LoadFileCoreAsync(path));
    }

    /// <summary>
    /// 载入主体（不自带忙碌闸门）：由调用方包装 —— 单文件命令与
    /// 工程树切换（切换前先执行防丢失保存）共用同一条载入链。
    /// </summary>
    private async Task LoadFileCoreAsync(string path)
    {
        var client = SidecarClientProvider.Client;
        var detect = await client.DetectFormatAsync(path);
        if (!detect.Detected)
        {
            StatusMessage = "无适配器认领该文件的引擎格式。";
            return;
        }
        var envelope = await client.ExtractToIrAsync(path);
        _currentProject = envelope.Project;
        CurrentFilePath = path;
        ExportDirectory = ResolveExportDirectory();
        // 状态复位：否则上一个文件遗留的筛选（如 Failed）会把新数据整片滤空，
        // 表现为「加载失败」，实则筛选器所致。
        SearchText = string.Empty;
        FilterMode = "All";
        FiltersReset?.Invoke();
        ReplaceUnits(envelope.Project.Units.Select(u => new StudioUnitItemViewModel(u)));
        var glossaryError = await AttachGlossaryAsync(envelope.Project.Units);
        SelectedUnit = Units.FirstOrDefault();
        UnitsView.Refresh();
        var loadMessage = glossaryError is null
            ? $"已加载 {envelope.UnitCount} 条可译文本（引擎：{detect.Adapter}）。"
            : $"已加载 {envelope.UnitCount} 条；术语匹配失败：{glossaryError}";
        StatusMessage = _pendingSaveNote is null
            ? loadMessage
            : $"{loadMessage} {_pendingSaveNote}";
        _pendingSaveNote = null;
    }

    /// <summary>
    /// 批量翻译闭环：切片送翻 → 逐片回填 → **对已获译文的行复检 LQA** → 推进进度。
    ///
    /// 默认 API Base 指向本地环回 Mock（127.0.0.1:18080/v1），零额度离线演练。
    ///
    /// 三重保护：
    /// * <see cref="IsTranslating"/> 防重入，另有 <see cref="CanTranslateBatch"/> 让按钮置灰；
    /// * 切片而非整批发送 —— 整批一次发出去，用户在整个往返期间只能看到进度条静止；
    /// * 只翻「已抽取且译文空白」的行 —— LQA_FAILED 携带的是**有效诊断**
    ///   （引号未配平之类），重翻它属于单行 Re-try 的职责，批量不该静默覆盖。
    /// </summary>
    [RelayCommand]
    private async Task TranslateBatchAsync()
    {
        if (IsTranslating || !CanTranslateBatch)
        {
            return;
        }
        // 先全量提交人工微调：否则抽取期快照会盖掉用户刚敲进去的译文，
        // 也会让「待译判定」与实际送翻内容不一致。
        foreach (var unit in Units)
        {
            unit.CommitEdits();
        }
        var targets = Units.Where(u => u.NeedsTranslation).ToList();
        if (targets.Count == 0)
        {
            StatusMessage = "没有待翻译单元。";
            return;
        }
        IsTranslating = true;
        try
        {
            await RunBusyAsync(() => RunBatchTranslationAsync(targets));
        }
        finally
        {
            IsTranslating = false;
        }
    }

    /// <summary>
    /// 从进程级设置仓库组装翻译调度配置 —— Settings 页是唯一真源。
    /// 批量与单条重试共用此实现，杜绝两处口径漂移。
    /// </summary>
    private static TranslationConfigDto CreateTranslationConfig()
    {
        var settings = TranslationSettingsStore.Current;
        return new TranslationConfigDto(
            ApiBase: settings.ApiBase,
            ModelName: settings.ModelName,
            ApiKey: settings.ApiKey,
            Temperature: settings.Temperature,
            ReasoningEffort: settings.ReasoningEffort);
    }

    /// <summary>滑窗上下文行数（与 Python 端 TranslationConfig.context_lines 默认值一致）。</summary>
    private const int ContextWindowLines = 4;

    /// <summary>本会话累计真实 Token 消耗（每片/每条重试的实际 usage 原子累加；无消耗恒为 0）。</summary>
    [ObservableProperty]
    public partial int SessionTotalTokens { get; set; }

    /// <summary>底栏会话消耗文案：无消耗时为空串（不显示），绝不伪造数字。</summary>
    public string SessionTokensText =>
        SessionTotalTokens > 0 ? $"Session: {SessionTotalTokens:#,0} Tokens" : string.Empty;

    /// <summary>把一批次回传的真实 usage 并入会话累计（Sidecar 已跨子批原子累加）。</summary>
    private void AccumulateSessionTokens(TokenUsageDto? usage)
    {
        if (usage is null || usage.TotalTokens <= 0)
        {
            return;
        }
        SessionTotalTokens += usage.TotalTokens;
    }

    partial void OnSessionTotalTokensChanged(int value)
    {
        OnPropertyChanged(nameof(SessionTokensText));
        PublishMetrics();
    }

    /// <summary>
    /// 构建滑窗上下文：按文档序取最近 <see cref="ContextWindowLines"/> 行**已有译文**的
    /// 对白（角色 + 译文）。待译目标尚无译文，天然不会混入；同一次运行里
    /// 先完成的切片经回填后，会成为后续切片的语境参考。
    /// </summary>
    private List<TranslationContextLine> BuildBatchContext() =>
        Units
        .Where(u => !string.IsNullOrWhiteSpace(u.TranslatedText))
        .Select(u => new TranslationContextLine(u.Speaker, u.TranslatedText.Trim()))
        .TakeLast(ContextWindowLines)
        .ToList();

    /// <summary>批量翻译主体：切片往返 + 回填 + 复检 + 进度推进（重入保护由调用方承载）。</summary>
    private async Task RunBatchTranslationAsync(List<StudioUnitItemViewModel> targets)
    {
        var total = targets.Count;
        var chunkSize = Math.Max(1, TranslateChunkSize);
        var config = CreateTranslationConfig();
        var stopwatch = Stopwatch.StartNew();
        var completed = 0;

        StatusMessage = $"正在翻译 {total} 条（每片 {chunkSize}）…";
        PublishMetrics();

        for (var start = 0; start < total; start += chunkSize)
        {
            var chunk = targets.GetRange(start, Math.Min(chunkSize, total - start));
            var payload = chunk.Select(u => u.Model).ToList();

            // 不再强制 BatchSize = payload.Count（那会把 Sidecar 钳成单子批，
            // 并发池永远不生效）—— 让 Python 按 config.batch_size 二次切分，
            // 以 concurrency_limit 并发跑满本切片；
            // 滑窗上下文按当前文档序取「已定稿前文」：先完成的切片天然成为
            // 后续切片的语境（代词指代 / 语气衔接）。
            var updated = await SidecarClientProvider.Client.TranslateBatchAsync(
                payload, config, BuildBatchContext());
            BackfillUnits(chunk, updated.Units);
            AccumulateSessionTokens(updated.Usage);

            // 已获译文的行立即复检：translator 的内联门禁与独立 run_lqa 在
            // 「清除陈旧违例留痕」上的职责不同，这一步把状态与 metadata 收敛到权威值。
            // 译文为空的行刻意跳过：它们由 translate_batch 判为 LQA_FAILED
            // （批次响应缺失可用译文），若一并送检会被规整成 EXTRACTED，洗掉这个信号。
            var received = chunk
                .Select(u => u.Model)
                .Where(u => !string.IsNullOrWhiteSpace(u.TranslatedText))
                .ToList();
            if (received.Count > 0)
            {
                BackfillUnits(chunk, await SidecarClientProvider.Client.RunLqaAsync(received));
            }

            completed += chunk.Count;
            RefreshProgress();
            StatusMessage = $"正在翻译… {completed}/{total}（通过 {PassedCount} / 不合格 {FailedCount}）";
        }

        stopwatch.Stop();
        LastLatencyMs = stopwatch.Elapsed.TotalMilliseconds;
        PublishMetrics();
        StatusMessage =
            $"批量翻译完成：{completed} 条，耗时 {stopwatch.ElapsedMilliseconds:#,0}ms"
            + $"（通过 {PassedCount} / 不合格 {FailedCount}）。";
    }

    /// <summary>
    /// 按 id 把 Sidecar 回传的单元原子刷回行 VM。
    /// 用 <see cref="_suspendStats"/> 抑制逐行触发 —— 一次切片 N 行只需在末尾聚合一次，
    /// 否则回填 N 行会退化成 O(n²) 的全表重算。
    /// </summary>
    private void BackfillUnits(
        IReadOnlyList<StudioUnitItemViewModel> rows,
        IReadOnlyList<TranslationUnitDto> updated)
    {
        if (updated.Count == 0)
        {
            return;
        }
        // 不去重即抛的 ToDictionary 会在协议异常时把整个批次打挂；后写覆盖更稳。
        var byId = new Dictionary<string, TranslationUnitDto>(StringComparer.Ordinal);
        foreach (var dto in updated)
        {
            byId[dto.Id] = dto;
        }
        _suspendStats = true;
        try
        {
            foreach (var row in rows)
            {
                if (byId.TryGetValue(row.Id, out var dto))
                {
                    row.Update(dto);
                }
            }
        }
        finally
        {
            _suspendStats = false;
        }
    }

    /// <summary>单条重试：仅重翻该行（Failed 行「Re-try」快捷动作）。</summary>
    [RelayCommand]
    private async Task RetryUnitAsync(StudioUnitItemViewModel? item)
    {
        if (item is null)
        {
            return;
        }
        await RunBusyAsync(async () =>
        {
            // 滑窗前文排除被重试行自身：它携带的正是要被推翻的旧译文，
            // 注入回去反而会把模型锚定在错误译法上
            var context = Units
                .Where(u => !string.IsNullOrWhiteSpace(u.TranslatedText)
                            && !ReferenceEquals(u, item))
                .Select(u => new TranslationContextLine(u.Speaker, u.TranslatedText.Trim()))
                .TakeLast(ContextWindowLines)
                .ToList();
            var updated = await SidecarClientProvider.Client.TranslateBatchAsync(
                [item.Model], CreateTranslationConfig(), context);
            // 走统一回填：原先的 Units.First(...) 在协议异常时会抛，整条命令直接报错
            BackfillUnits([item], updated.Units);
            AccumulateSessionTokens(updated.Usage);
            RefreshProgress();
            StatusMessage = $"已重试单元 {item.LineNumberTag}（{item.StatusDisplay}）。";
        });
    }

    /// <summary>
    /// 导出回写：先把全部行 VM 的人工编辑全量同步进 Model（既定教训：漏掉
    /// 全量 CommitEdits，抽取期快照会静默覆盖人工微调），再以行 VM 的最新
    /// Model 重建项目信封，经 ir_to_asset 按行号原位还原为目标引擎脚本。
    /// 成功后把参与单元推进 EXPORTED，并把实际输出路径回显到底栏。
    /// </summary>
    [RelayCommand]
    private async Task ExportScriptAsync()
    {
        if (_currentProject is null)
        {
            StatusMessage = "尚无已载入的剧本，请先载入文件再导出。";
            return;
        }
        await RunBusyAsync(async () =>
        {
            foreach (var unit in Units)
            {
                unit.CommitEdits();
            }
            var result = await ExportProjectCoreAsync();
            // 闭环终点落点：契约里的 EXPORTED 此前全仓库零写入，导出成功无处标记
            foreach (var unit in Units)
            {
                unit.MarkExported();
            }
            RefreshProgress();
            StatusMessage = $"导出成功：{result.OutputPath}";
        });
    }

    /// <summary>
    /// 导出主体（不自带忙碌闸门与状态文案）：显式导出与切换剧本时的
    /// 自动保存共用同一条 ir_to_asset 管道 —— 不存在两套导出口径。
    /// </summary>
    private async Task<IrToAssetResult> ExportProjectCoreAsync()
    {
        var project = _currentProject
            ?? throw new InvalidOperationException("ExportProjectCoreAsync 要求已载入工程（调用方已守卫）");
        var exportDir = ResolveExportDirectory();
        project = project with { Units = Units.Select(u => u.Model).ToList() };
        var stopwatch = Stopwatch.StartNew();
        var result = await SidecarClientProvider.Client.IrToAssetAsync(project, exportDir);
        stopwatch.Stop();
        LastLatencyMs = stopwatch.Elapsed.TotalMilliseconds;
        ExportDirectory = exportDir;
        LastExportPath = result.OutputPath;
        return result;
    }

    /// <summary>
    /// 人工内联审校后的**即时质检**（行失焦触发）：CommitEdits 已把最新译文
    /// 写回 DTO，此处把该行送 Sidecar 重跑静态 LQA 规则（引号配平 / 破折号成双 /
    /// 宏锚点守恒），并以返回值原子刷新状态胶囊与错误气泡。
    ///
    /// 刻意不走 <see cref="RunBusyAsync"/>：审校时会连续切行，全局 IsBusy 会
    /// 令后续复检被 <c>if (IsBusy) return</c> 静默丢弃；此路径轻量、可并发，
    /// 失败仅提示降级，绝不打断编辑流。
    /// </summary>
    public async Task RecheckUnitAsync(StudioUnitItemViewModel? item)
    {
        if (item is null)
        {
            return;
        }
        try
        {
            var refreshed = await SidecarClientProvider.Client.RunLqaAsync([item.Model]);
            foreach (var dto in refreshed)
            {
                item.Update(dto);
            }
            RefreshProgress();
            StatusMessage = item.IsFailed
                ? $"第 {item.LineNumberTag} 行未通过质检：{item.IssueTooltip}"
                : $"第 {item.LineNumberTag} 行质检通过。";
        }
        catch (JsonRpcException ex)
        {
            StatusMessage = $"质检失败 [{ex.Code}]：{ex.Message}";
        }
        catch (Exception ex)
        {
            StatusMessage = $"质检失败：{ex.Message}";
        }
    }

    private async Task RunBusyAsync(Func<Task> work)
    {
        if (IsBusy)
        {
            return;
        }
        IsBusy = true;
        try
        {
            await work();
        }
        catch (JsonRpcException ex)
        {
            StatusMessage = $"Sidecar 业务错误 [{ex.Code}]：{ex.Message}";
        }
        catch (Exception ex)
        {
            StatusMessage = $"操作失败：{ex.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>
    /// 单次整表聚合，刷新全部统计量并广播派生属性。
    /// 所有计数均来自共享库 <see cref="TranslationProgress.Summarize"/> ——
    /// 「按钮角标」「批量选取目标」「进度条」三处口径同源，不可能互相漂移。
    /// </summary>
    private void RefreshProgress()
    {
        if (_suspendStats)
        {
            return;
        }
        _snapshot = TranslationProgress.Summarize(
            Units.Select(u => ((string?)u.Status, (string?)u.TranslatedText)));
        TotalCount = _snapshot.Total;
        TranslatedCount = _snapshot.Translated;
        PassedCount = _snapshot.Passed;
        FailedCount = _snapshot.Failed;
        PendingTranslationCount = _snapshot.Pending;
        OnPropertyChanged(nameof(TranslationProgressRatio));
        OnPropertyChanged(nameof(TranslationProgressText));
        OnPropertyChanged(nameof(BatchTranslateLabel));
        OnPropertyChanged(nameof(ProgressLabel));
        PublishMetrics();
    }

    /// <summary>整批替换单元集：解绑旧行、绑定新行的 PropertyChanged，供统计实时刷新。</summary>
    private void ReplaceUnits(IEnumerable<StudioUnitItemViewModel> rows)
    {
        foreach (var old in Units)
        {
            old.PropertyChanged -= OnUnitPropertyChanged;
        }
        Units.Clear();
        _suspendStats = true;
        try
        {
            foreach (var row in rows)
            {
                row.PropertyChanged += OnUnitPropertyChanged;
                Units.Add(row);
            }
        }
        finally
        {
            _suspendStats = false;
        }
        RefreshProgress();
    }

    /// <summary>
    /// 行级变更 → 统计重算。只监听会影响计数的两个属性：译文（进度分子与待译判定）
    /// 与状态（通过 / 失败 / 终态判定）。其余属性（如徽章底色）变化不触发全表聚合。
    /// </summary>
    private void OnUnitPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(StudioUnitItemViewModel.TranslatedText)
            or nameof(StudioUnitItemViewModel.Status))
        {
            RefreshProgress();
        }
    }

    /// <summary>
    /// 底栏指标串 —— **只承载真实观测值**：实测延迟，以及导出产物的路径回显。
    ///
    /// 原先此处还拼了一个 <c>TokenUsageText</c>（写死 "Session Tokens: 142.5k"），
    /// 已彻底删除：`translate_batch` 的响应契约是单元列表，**不回传 usage**，
    /// 壳层无任何真实 Token 数据可观测 —— 显示一个编造的读数比留空更糟。
    /// 要让 Token 计量成真，需要 translate_batch 改返回信封并携带 usage
    /// （见 docs/AI_HANDOFF.md 后续路线）。
    /// </summary>
    private string BuildMetricsLabel()
    {
        var segments = new List<string>();
        if (!string.IsNullOrEmpty(SessionTokensText))
        {
            segments.Add(SessionTokensText);
        }
        segments.Add(LatencyText);
        if (!string.IsNullOrWhiteSpace(LastExportPath))
        {
            segments.Add($"Export: {ExportDisplayLabel}");
        }
        return string.Join("    ", segments);
    }

    /// <summary>导出产物的紧凑展示串（`目录/文件名`），避免长绝对路径挤爆底栏。</summary>
    private string ExportDisplayLabel
    {
        get
        {
            if (string.IsNullOrWhiteSpace(LastExportPath))
            {
                return string.Empty;
            }
            var file = Path.GetFileName(LastExportPath);
            var dir = Path.GetFileName(Path.GetDirectoryName(LastExportPath) ?? string.Empty);
            return string.IsNullOrWhiteSpace(dir) ? file : $"{dir}/{file}";
        }
    }

    private void PublishMetrics() =>
        MetricsSink?.Invoke(FileStatusLabel, BuildMetricsLabel(), StatusMessage);

    /// <summary>
    /// 载入后装配术语命中：整表一次 RPC，再按 id 分发到各行。
    ///
    /// 之所以能整表一次取回而非选中行时现查：匹配依据是 <c>extracted_text</c>（原文），
    /// 原文在抽取后不再变化 —— 缓存一次即可让 Inspector 在切行时零往返。
    /// 失败不阻断载入（术语是增益信息，不是载入前置条件），返回失败原因供调用方
    /// 拼进状态文案 —— 若在这里直接写 StatusMessage，会被随后的载入成功文案盖掉。
    /// </summary>
    private async Task<string?> AttachGlossaryAsync(IReadOnlyList<TranslationUnitDto> units)
    {
        try
        {
            var matches = await SidecarClientProvider.Client.MatchGlossaryAsync(units);
            foreach (var row in Units)
            {
                row.ApplyGlossary(matches.TryGetValue(row.Id, out var hit) ? hit : null);
            }
            return null;
        }
        catch (JsonRpcException ex)
        {
            foreach (var row in Units)
            {
                row.ApplyGlossary(null);
            }
            return $"[{ex.Code}] {ex.Message}";
        }
        catch (Exception ex)
        {
            foreach (var row in Units)
            {
                row.ApplyGlossary(null);
            }
            return ex.Message;
        }
    }

    public void Dispose()
    {
        CancelPendingTranscode();
        _unpackCts?.Cancel();
        _audioPlayer.Dispose();
        _currentProject = null;
    }
}
