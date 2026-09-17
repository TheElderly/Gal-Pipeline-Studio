using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using GalPipeline.Core.Inspector;

namespace GalPipeline.Core.IPC;

/// <summary>Python Sidecar 业务/协议错误：Code 为 JSON-RPC 2.0 规范错误码（-32000 业务约定等）。</summary>
public sealed class JsonRpcException(int code, string message) : Exception(message)
{
    public int Code { get; } = code;
}

// ---------------------------------------------------------------------------
// Gal-IR 信封模型（对齐 core/server/rpc.py 的 snake_case 序列化契约）
// ---------------------------------------------------------------------------

public sealed record AtomicTagDto(string TagId, string RawTag, long Position);

public sealed record PairedTagDto(string StartTag, string EndTag, string InnerText);

public sealed record TranslationUnitDto(
    string Id,
    string? Speaker,
    string RawText,
    string ExtractedText,
    List<AtomicTagDto> AtomicTags,
    List<PairedTagDto> PairedTags,
    string? TranslatedText,
    string Status,
    Dictionary<string, JsonElement>? Metadata);

public sealed record GalIRProjectDto(
    string ProjectName,
    string SourceLang,
    string TargetLang,
    string EngineType,
    List<TranslationUnitDto> Units);

public sealed record ExtractStats(int Total, Dictionary<string, int> ByStatus);

/// <summary>extract_to_ir 响应信封：Project 可直接回灌 IrToAssetAsync。</summary>
public sealed record ExtractToIrResult(
    GalIRProjectDto Project, string ProjectId, int UnitCount, ExtractStats Stats);

public sealed record DetectFormatResult(bool Detected, string Adapter);

/// <summary>detect_engine 响应：模块 0 的引擎画像（与 Python pydantic 模型逐字段对齐）。</summary>
public sealed record EngineEvidenceDto(string EngineId, double Weight, string Description);

public sealed record EngineProfileDto(
    string GameDir,
    bool Detected,
    string? EngineType,
    string? EngineDisplay,
    double Confidence,
    List<EngineEvidenceDto> Evidence,
    string? RecommendedUnpacker,
    int ScannedExes);

public sealed record InitWorkspaceResult(string WorkspaceRoot, bool Existed, EngineProfileDto Profile);

/// <summary>detect_engine 响应信封：{"profile": {...}}。</summary>
public sealed record EngineProfileResult(EngineProfileDto Profile);

/// <summary>list_archives 响应：待解包封包（相对游戏目录的路径，自然序）。</summary>
public sealed record ListArchivesResult(List<string> Archives);

/// <summary>unpack_archive 响应：单封包解包交付（skipped=true 表示幂等跳过）。</summary>
public sealed record UnpackArchiveResult(string OutputDir, int FileCount, bool Skipped);

public sealed record FetchModelsResult(List<string> Models);

public sealed record IrToAssetResult(string OutputPath);

/// <summary>match_glossary 响应信封：unit_id → 术语命中列表（无命中的单元被省略）。</summary>
public sealed record GlossaryMatchResult(Dictionary<string, List<GlossaryMatch>> Matches);

public sealed record TranslationConfigDto(
    string ApiBase,
    string ModelName,
    string? ApiKey = null,
    double Temperature = 0.3,
    int? BatchSize = null,
    double? TimeoutSeconds = null,
    int? MaxRetries = null,
    string? ReasoningEffort = null);

/// <summary>滑窗上下文行：一段「已定稿前文对白」的角色与译文（仅供语境参考）。</summary>
public sealed record TranslationContextLine(string? Speaker, string Text);

/// <summary>一次翻译批次的真实 Token 消耗（Sidecar 跨子批原子累加后回传）。</summary>
public sealed record TokenUsageDto(int PromptTokens, int CompletionTokens, int TotalTokens);

/// <summary>translate_batch 响应信封：状态刷新后的单元列表 + 本批次真实消耗。</summary>
public sealed record TranslateBatchResult(List<TranslationUnitDto> Units, TokenUsageDto? Usage);

/// <summary>convert_image 响应：转换产物路径与图像元数据。</summary>
public sealed record ConvertImageResult(string DstPath, int Width, int Height, string Format);

// ---------------------------------------------------------------------------
// 客户端
// ---------------------------------------------------------------------------

/// <summary>
/// Python Sidecar 的 StdIO JSON-RPC 2.0 客户端：无窗口托管子进程、
/// 异步行式收发、TaskCompletionSource 按 id 匹配并发响应。
/// </summary>
public sealed class PythonSidecarClient : IDisposable
{
    private static readonly JsonSerializerOptions DtoOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private const int RequestTimeoutSeconds = 60;

    private readonly Process _process;
    private readonly StreamWriter _writer;
    private readonly CancellationTokenSource _lifecycleCts = new();
    private readonly ConcurrentDictionary<long, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly SemaphoreSlim _writeGate = new(1, 1); // StreamWriter 拒绝交叠异步写，帧必须串行
    private long _nextId;
    private bool _disposed;

    /// <summary>拉起 Python Sidecar（python -X utf8 -m core.server.rpc，无窗口、重定向 StdIO）。</summary>
    public PythonSidecarClient(string? workingDirectory = null, string pythonExecutable = "python")
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = pythonExecutable,
            Arguments = "-X utf8 -m core.server.rpc",
            WorkingDirectory = workingDirectory ?? LocateRepositoryRoot(),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
            StandardErrorEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
        };
        _process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Python Sidecar 进程启动失败");
        // BOM 必须禁用：行首 BOM 会令 Python json.loads 抛 Parse error
        _writer = new StreamWriter(_process.StandardInput.BaseStream, new UTF8Encoding(false))
        {
            AutoFlush = true,
        };
        _ = _process.StandardError.ReadToEndAsync(); // 排干 stderr 防管道塞死（traceback 等）
        _ = Task.Run(() => ReadLoopAsync(_lifecycleCts.Token));
    }

    public async Task<string> PingAsync(CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("ping", null, cancellationToken).ConfigureAwait(false);
        return result.GetString() ?? throw new JsonRpcException(-32603, "ping 响应非字符串");
    }

    public async Task<DetectFormatResult> DetectFormatAsync(
        string filePath, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("detect_format", new { file_path = filePath }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<DetectFormatResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "detect_format 响应结构异常");
    }

    /// <summary>模块 0：对游戏根目录做只读指纹侦察（detect_engine 跨进程转发）。</summary>
    public async Task<EngineProfileDto> DetectEngineAsync(
        string gameDir, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("detect_engine", new { game_dir = gameDir }, cancellationToken)
            .ConfigureAwait(false);
        var profile = result.Deserialize<EngineProfileResult>(DtoOptions)?.Profile
            ?? throw new JsonRpcException(-32603, "detect_engine 响应结构异常");
        return profile;
    }

    /// <summary>模块 1：在游戏根目录创建标准 .galpipeline 工作区（幂等，不覆盖既有元数据）。</summary>
    public async Task<InitWorkspaceResult> InitWorkspaceAsync(
        string gameDir, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("init_workspace", new { game_dir = gameDir }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<InitWorkspaceResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "init_workspace 响应结构异常");
    }

    /// <summary>模块 1 编排第一步：列出待解包封包（每封包一条短 RPC 的调度清单）。</summary>
    public async Task<ListArchivesResult> ListArchivesAsync(
        string gameDir, string? engineType, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync(
                "list_archives", new { game_dir = gameDir, engine_type = engineType },
                cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<ListArchivesResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "list_archives 响应结构异常");
    }

    /// <summary>
    /// 模块 1 编排第二步：解包单个封包（工具路径由统一工具链解析后传入）。
    /// 逐封包短 RPC：取消粒度=封包，进度=已处理/总数，单封包失败可跳过续走。
    /// </summary>
    public async Task<UnpackArchiveResult> UnpackArchiveAsync(
        string gameDir,
        string? engineType,
        string archive,
        string toolPath,
        CancellationToken cancellationToken = default)
    {
        var result = await SendAsync(
                "unpack_archive",
                new { game_dir = gameDir, engine_type = engineType,
                      archive = archive, tool_path = toolPath },
                cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<UnpackArchiveResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "unpack_archive 响应结构异常");
    }

    /// <summary>拉取 OpenAI 兼容端点的模型清单（fetch_models 跨进程转发）。</summary>
    public async Task<List<string>> FetchModelsAsync(
        string apiBase, string? apiKey = null, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync(
            "fetch_models",
            new { api_base = apiBase, api_key = apiKey },
            cancellationToken).ConfigureAwait(false);
        return result.Deserialize<FetchModelsResult>(DtoOptions)?.Models
            ?? throw new JsonRpcException(-32603, "fetch_models 响应结构异常");
    }

    public async Task<ExtractToIrResult> ExtractToIrAsync(
        string filePath, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("extract_to_ir", new { file_path = filePath }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<ExtractToIrResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "extract_to_ir 响应结构异常");
    }

    public Task<TranslateBatchResult> TranslateBatchAsync(
        IEnumerable<TranslationUnitDto> units,
        TranslationConfigDto config,
        IReadOnlyList<TranslationContextLine>? context = null,
        CancellationToken cancellationToken = default)
        => SendAsync("translate_batch", new { units, config, context }, cancellationToken)
            .ContinueWith(t =>
                t.Result.Deserialize<TranslateBatchResult>(DtoOptions)
                ?? throw new JsonRpcException(-32603, "translate_batch 响应结构异常"),
                cancellationToken, TaskContinuationOptions.OnlyOnRanToCompletion, TaskScheduler.Default);

    /// <summary>私有图像转换（TLG5 内置 / 其他变体走外部工具链通道）。</summary>
    public Task<ConvertImageResult> ConvertImageAsync(
        string srcPath,
        string dstPath,
        string? toolPath = null,
        CancellationToken cancellationToken = default)
        => SendAsync("convert_image", new { src_path = srcPath, dst_path = dstPath, tool_path = toolPath }, cancellationToken)
            .ContinueWith(t =>
                t.Result.Deserialize<ConvertImageResult>(DtoOptions)
                ?? throw new JsonRpcException(-32603, "convert_image 响应结构异常"),
                cancellationToken, TaskContinuationOptions.OnlyOnRanToCompletion, TaskScheduler.Default);

    public async Task<IrToAssetResult> IrToAssetAsync(
        GalIRProjectDto project, string outputDir, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("ir_to_asset", new { project, output_dir = outputDir }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<IrToAssetResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "ir_to_asset 响应结构异常");
    }

    /// <summary>
    /// 人工内联审校后的即时质检：送 Sidecar 重跑静态 LQA 规则，
    /// 返回 status 与 metadata["lqa_issues"] 已刷新的单元副本。
    /// </summary>
    public async Task<List<TranslationUnitDto>> RunLqaAsync(
        IEnumerable<TranslationUnitDto> units, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("run_lqa", new { units }, cancellationToken).ConfigureAwait(false);
        return result.Deserialize<List<TranslationUnitDto>>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "run_lqa 响应结构异常");
    }

    /// <summary>
    /// 批量匹配术语表：返回 unit_id → 命中列表（无命中的单元不出现在结果里）。
    ///
    /// 匹配依据是原文（extracted_text），而原文在抽取后不再变化 ——
    /// 故载入时一次取回、整表缓存即可，选中行时零往返，Inspector 才能即时响应。
    /// </summary>
    public async Task<Dictionary<string, List<GlossaryMatch>>> MatchGlossaryAsync(
        IEnumerable<TranslationUnitDto> units, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("match_glossary", new { units }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<GlossaryMatchResult>(DtoOptions)?.Matches ?? [];
    }

    /// <summary>优雅关闭：先关 StdIn 触发 Python EOF 自然退出，超时再整树强杀。</summary>
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _lifecycleCts.Cancel();
        try
        {
            _writer.Close(); // EOF → serve_stdio() 循环自然退出
        }
        catch (IOException)
        {
        }
        if (!_process.WaitForExit(3000))
        {
            try
            {
                _process.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException)
            {
            }
        }
        _process.Dispose();
        _writeGate.Dispose();
        _lifecycleCts.Dispose();
        FaultPending("Sidecar 客户端已释放");
        GC.SuppressFinalize(this);
    }

    private async Task<JsonElement> SendAsync(
        string method, object? parameters, CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var id = Interlocked.Increment(ref _nextId);
        var completion = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = completion;
        try
        {
            var frame = JsonSerializer.Serialize(
                new { jsonrpc = "2.0", id, method, @params = parameters ?? (object)new { } }, DtoOptions);
            await _writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                await _writer.WriteLineAsync(frame).ConfigureAwait(false);
            }
            finally
            {
                _writeGate.Release();
            }
            return await completion.Task
                .WaitAsync(TimeSpan.FromSeconds(RequestTimeoutSeconds), cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    private async Task ReadLoopAsync(CancellationToken cancellationToken)
    {
        try
        {
            while (await _process.StandardOutput.ReadLineAsync(cancellationToken).ConfigureAwait(false)
                   is { } line)
            {
                if (!string.IsNullOrWhiteSpace(line))
                {
                    HandleLine(line);
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception)
        {
            // 管道断裂（进程退出/被杀）：待决请求统一失败
        }
        FaultPending("Sidecar 进程已退出");
    }

    private void HandleLine(string line)
    {
        JsonElement frame;
        try
        {
            frame = JsonSerializer.Deserialize<JsonElement>(line);
        }
        catch (JsonException)
        {
            return; // 非法响应帧丢弃（服务端契约保证不会出现）
        }
        if (frame.ValueKind is not JsonValueKind.Object
            || !frame.TryGetProperty("id", out var idElement)
            || idElement.ValueKind is not JsonValueKind.Number
            || !idElement.TryGetInt64(out var id)
            || !_pending.TryRemove(id, out var completion))
        {
            return;
        }
        if (frame.TryGetProperty("error", out var error))
        {
            var code = error.TryGetProperty("code", out var codeElement)
                && codeElement.ValueKind is JsonValueKind.Number
                    ? codeElement.GetInt32() : -32603;
            var message = error.TryGetProperty("message", out var messageElement)
                && messageElement.ValueKind is JsonValueKind.String
                    ? messageElement.GetString()! : "未知错误";
            completion.TrySetException(new JsonRpcException(code, message));
            return;
        }
        completion.TrySetResult(
            frame.TryGetProperty("result", out var result)
                ? result
                : JsonSerializer.SerializeToElement(new { }));
    }

    private void FaultPending(string reason)
    {
        foreach (var pair in _pending)
        {
            if (_pending.TryRemove(pair.Key, out var completion))
            {
                completion.TrySetException(new JsonRpcException(-32603, reason));
            }
        }
    }

    /// <summary>自测试程序集目录向上定位仓库根（以 core/server/rpc.py 为锚点）。</summary>
    private static string LocateRepositoryRoot()
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
        throw new InvalidOperationException("未找到仓库根：缺少 core/server/rpc.py 锚点");
    }
}
