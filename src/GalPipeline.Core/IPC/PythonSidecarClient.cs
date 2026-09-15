using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

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

public sealed record IrToAssetResult(string OutputPath);

public sealed record TranslationConfigDto(
    string ApiBase,
    string ModelName,
    string? ApiKey = null,
    double? Temperature = null,
    int? BatchSize = null,
    double? TimeoutSeconds = null,
    int? MaxRetries = null);

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

    public async Task<ExtractToIrResult> ExtractToIrAsync(
        string filePath, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("extract_to_ir", new { file_path = filePath }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<ExtractToIrResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "extract_to_ir 响应结构异常");
    }

    public Task<List<TranslationUnitDto>> TranslateBatchAsync(
        IEnumerable<TranslationUnitDto> units,
        TranslationConfigDto config,
        CancellationToken cancellationToken = default)
        => SendAsync("translate_batch", new { units, config }, cancellationToken)
            .ContinueWith(t =>
                t.Result.Deserialize<List<TranslationUnitDto>>(DtoOptions)
                ?? throw new JsonRpcException(-32603, "translate_batch 响应结构异常"),
                cancellationToken, TaskContinuationOptions.OnlyOnRanToCompletion, TaskScheduler.Default);

    public async Task<IrToAssetResult> IrToAssetAsync(
        GalIRProjectDto project, string outputDir, CancellationToken cancellationToken = default)
    {
        var result = await SendAsync("ir_to_asset", new { project, output_dir = outputDir }, cancellationToken)
            .ConfigureAwait(false);
        return result.Deserialize<IrToAssetResult>(DtoOptions)
            ?? throw new JsonRpcException(-32603, "ir_to_asset 响应结构异常");
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
