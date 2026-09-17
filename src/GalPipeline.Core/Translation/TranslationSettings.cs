namespace GalPipeline.Core.Translation;

using GalPipeline.Core.Toolchain;

/// <summary>
/// 翻译调度配置快照（不可变）。Settings 页写入、Studio 批量/单条重试读取，
/// 双方消费同一份实例 —— 消灭「设置页调了、翻译仍走写死默认值」的软花架子。
/// </summary>
/// <param name="ApiBase">OpenAI 兼容端点根地址。</param>
/// <param name="ApiKey">凭据；空串按匿名处理（本地环回 Mock 常用）。</param>
/// <param name="ModelName">目标模型名。</param>
/// <param name="Temperature">采样温度 ∈ [0, 2]。</param>
/// <param name="ReasoningEffort">推理力度；null 表示不注入载荷（非推理模型必选）。</param>
/// <param name="CustomToolPaths">
/// 用户自定义外部工具路径映射（设置页工具链卡片写入）。
/// **null 语义 = 未指定**（不参与比较、合并时视为空映射）；条目存在但值为
/// null/空白 = 该工具的自定义路径已清除，回退瀑布探测。解析失败时
/// <see cref="ExternalToolResolver"/> 自动继续瀑布，不会挡住本地工具箱副本。
/// </param>
public sealed record TranslationSettingsSnapshot(
    string ApiBase,
    string? ApiKey,
    string ModelName,
    double Temperature,
    string? ReasoningEffort,
    IReadOnlyDictionary<ToolType, string>? CustomToolPaths = null)
{
    /// <summary>以不可变方式增改一个工具的自定义路径（path 空白 = 清除）。</summary>
    public TranslationSettingsSnapshot WithCustomToolPath(ToolType type, string? path)
    {
        var merged = new Dictionary<ToolType, string>(CustomToolPaths ?? new Dictionary<ToolType, string>());
        if (string.IsNullOrWhiteSpace(path))
        {
            merged.Remove(type);
        }
        else
        {
            merged[type] = path.Trim();
        }
        return this with { CustomToolPaths = merged };
    }

    /// <summary>按内容比较自定义路径映射（字典默认是引用相等，快照合并语义必须按内容）。</summary>
    public static bool ToolPathsEqual(
        IReadOnlyDictionary<ToolType, string>? left, IReadOnlyDictionary<ToolType, string>? right)
    {
        if ((left is null || left.Count == 0) && (right is null || right.Count == 0))
        {
            return true;
        }
        if (left is null || right is null || left.Count != right.Count)
        {
            return false;
        }
        foreach (var (key, value) in left)
        {
            if (!right.TryGetValue(key, out var other) || !string.Equals(value, other, StringComparison.Ordinal))
            {
                return false;
            }
        }
        return true;
    }
}

/// <summary>
/// 进程级翻译设置仓库（单写多读，支持**作用域覆盖**）。
///
/// 为什么是静态仓库而不是互持引用：SettingsViewModel 随 Frame 导航反复
/// 重建（每次进设置页 new 一份），StudioViewModel 由 StudioPage 持有 ——
/// 二者没有稳定的对象通路。静态快照 + 「命令发起时读取」的时序，
/// 让改动在下一次批量翻译必然生效，不存在缓存失效问题。
///
/// 多工程污染防线（预留的依赖注入通道）：<see cref="BeginScope"/> 允许
/// 宿主为「某个工程/某个标签页」压入临时快照，作用域内的所有读取都
/// 得到覆盖值、释放即还原基准 —— 未来多标签页宿主只需把 BeginScope
/// 的生命周期绑定到标签页，跨工程配置就天然隔离；届时再把本仓库
/// 实例化并注入即可，消费方签名不变。
///
/// 默认值指向本地环回 Mock（离线红线：零额度、零外网），与
/// <c>tests/fixtures/mock_llm_server.py</c> 的约定地址一致。
/// </summary>
public static class TranslationSettingsStore
{
    public const string DefaultMockApiBase = "http://127.0.0.1:18080/v1";

    public const string DefaultModelName = "glm-4-flash";

    private static readonly object Gate = new();
    private static readonly Stack<TranslationSettingsSnapshot> Scopes = new();
    private static TranslationSettingsSnapshot _base = new(
        ApiBase: DefaultMockApiBase,
        ApiKey: null,
        ModelName: DefaultModelName,
        Temperature: 0.3,
        ReasoningEffort: null);

    /// <summary>当前生效快照：作用域栈顶优先，无作用域时回落基准。</summary>
    public static TranslationSettingsSnapshot Current
    {
        get
        {
            lock (Gate)
            {
                return Scopes.Count > 0 ? Scopes.Peek() : _base;
            }
        }
    }

    /// <summary>整体替换基准快照（原子引用切换，无半新半旧中间态）。
    /// 作用域激活期间的写入仍落基准 —— 作用域由宿主显式管理，页面写入
    /// 不应悄悄改写别的工程正在使用的覆盖值。</summary>
    public static void Update(TranslationSettingsSnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        lock (Gate)
        {
            _base = snapshot;
        }
    }

    /// <summary>
    /// 压入临时作用域：Dispose 前的所有 <see cref="Current"/> 读取得到
    /// <paramref name="snapshot"/>。用于多工程/多标签页的配置隔离（DI 预留通道）。
    /// </summary>
    public static IDisposable BeginScope(TranslationSettingsSnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        lock (Gate)
        {
            Scopes.Push(snapshot);
        }
        return new ScopeLease();
    }

    private sealed class ScopeLease : IDisposable
    {
        public void Dispose()
        {
            lock (Gate)
            {
                if (Scopes.Count > 0)
                {
                    Scopes.Pop();
                }
            }
        }
    }

    /// <summary>归一化凭据：空白视同匿名（避免空串被序列化成 "Bearer "）。</summary>
    public static string? NormalizeKey(string? apiKey) =>
        string.IsNullOrWhiteSpace(apiKey) ? null : apiKey.Trim();

    /// <summary>
    /// 工具链映射变更通知（仅在映射内容实际变化时触发一次）。
    /// <see cref="ToolchainRuntime"/> 订阅它重建解析器，使相关服务
    /// （FFmpeg 转码 / GARbro 解包 / TLG 转换）即刻消费最新工具状态。
    /// </summary>
    public static event Action? ToolchainChanged;

    /// <summary>
    /// 保存/清除单个工具的自定义路径（原子合并进当前基准快照，其余字段不动）。
    /// <paramref name="path"/> 为空白 = 清除该映射，回退瀑布探测。
    /// 写入恒落基准（与 <see cref="Update"/> 同语义：页面写入不越权改作用域覆盖值）。
    /// </summary>
    public static void SetCustomToolPath(ToolType type, string? path)
    {
        TranslationSettingsSnapshot replaced;
        lock (Gate)
        {
            replaced = _base.WithCustomToolPath(type, path);
            if (TranslationSettingsSnapshot.ToolPathsEqual(_base.CustomToolPaths, replaced.CustomToolPaths))
            {
                return; // 内容未变：不切换引用、不触发事件（防抖）
            }
            _base = replaced;
        }
        ToolchainChanged?.Invoke();
    }

    /// <summary>读取某工具当前的自定义路径（未设置返回 null）。</summary>
    public static string? GetCustomToolPath(ToolType type) =>
        Current.CustomToolPaths is { } paths && paths.TryGetValue(type, out var value)
            ? value
            : null;
}
