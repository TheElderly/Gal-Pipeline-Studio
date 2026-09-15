using System.Net;
using System.Net.Sockets;
using System.Text;
using GalPipeline.Core.IPC;
using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// Python Sidecar 集成测试：真实拉起 python -m core.server.rpc 进程，
/// 走完整 StdIO JSON-RPC 2.0 链路（非 Mock）。
/// </summary>
public class PythonSidecarClientTests
{
    private const string KagSample = "; 标题注释\n*begin\n【ヒロイン】おはよう[r]\n";

    [Fact]
    public async Task PingAsync_ReturnsPong()
    {
        using var client = new PythonSidecarClient();
        var reply = await client.PingAsync();
        Assert.Equal("pong", reply);
    }

    [Fact]
    public async Task PingAsync_ConcurrentRequests_MatchById()
    {
        using var client = new PythonSidecarClient();
        var pings = Enumerable.Range(0, 8).Select(_ => client.PingAsync());
        var replies = await Task.WhenAll(pings);
        Assert.All(replies, reply => Assert.Equal("pong", reply));
    }

    [Fact]
    public async Task DetectFormatAsync_MissingPath_ReturnsNotDetected()
    {
        using var client = new PythonSidecarClient();
        var result = await client.DetectFormatAsync(@"Z:\nonexistent\missing.ks");
        Assert.False(result.Detected);
        Assert.Equal(string.Empty, result.Adapter);
    }

    [Fact]
    public async Task ExtractToIrAsync_GarbageFile_MapsBusinessError32000()
    {
        var garbage = Path.Combine(Path.GetTempPath(), $"garbage-{Guid.NewGuid():N}.ks");
        await File.WriteAllTextAsync(garbage, "no kag features here\n", new UTF8Encoding(false));
        try
        {
            using var client = new PythonSidecarClient();
            var failure = await Assert.ThrowsAsync<JsonRpcException>(
                () => client.ExtractToIrAsync(garbage));
            Assert.Equal(-32000, failure.Code);
        }
        finally
        {
            File.Delete(garbage);
        }
    }

    [Fact]
    public async Task ExtractThenIrToAsset_RoundTrip_PreservesSkeletonAndBom()
    {
        var scene = Path.Combine(Path.GetTempPath(), $"scene-{Guid.NewGuid():N}.ks");
        var outputDir = Path.Combine(Path.GetTempPath(), $"out-{Guid.NewGuid():N}");
        await File.WriteAllTextAsync(scene, KagSample, new UTF8Encoding(false));
        try
        {
            using var client = new PythonSidecarClient();
            var extracted = await client.ExtractToIrAsync(scene);

            Assert.Equal("EXTRACTED", extracted.Project.Units[0].Status);
            Assert.Equal("ヒロイン", extracted.Project.Units[0].Speaker);

            var exported = await client.IrToAssetAsync(extracted.Project, outputDir);
            Assert.True(File.Exists(exported.OutputPath));
            var bytes = await File.ReadAllBytesAsync(exported.OutputPath);
            Assert.True(bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF,
                "导出文件缺少 UTF-8 BOM");
            var content = File.ReadAllText(exported.OutputPath, new UTF8Encoding(false));
            Assert.Contains("*begin", content);
            Assert.Contains("【ヒロイン】おはよう[r]", content);
        }
        finally
        {
            File.Delete(scene);
            if (Directory.Exists(outputDir))
            {
                Directory.Delete(outputDir, recursive: true);
            }
        }
    }
}

/// <summary>
/// 127.0.0.1 环回 OpenAI /models 桩：裸 TcpListener 应答，按路径分流
/// 成功 / 401 / 非 JSON，并记录 Authorization 头供断言。
/// </summary>
internal sealed class StubModelServer : IDisposable
{
    private readonly TcpListener _listener;
    private readonly CancellationTokenSource _cts = new();

    public int Port { get; }
    public string? LastAuth { get; private set; }

    public StubModelServer()
    {
        _listener = new TcpListener(IPAddress.Loopback, 0);
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _ = Task.Run(() => ServeAsync(_cts.Token));
    }

    private async Task ServeAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(ct);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            _ = Task.Run(() => HandleAsync(client, ct), ct);
        }
    }

    private async Task HandleAsync(TcpClient client, CancellationToken ct)
    {
        using var _ = client;
        var stream = client.GetStream();
        var request = await ReadRequestHeadAsync(stream, ct);
        var path = request.Split(' ').ElementAtOrDefault(1) ?? string.Empty;
        LastAuth = GetHeader(request, "Authorization");

        int code;
        string body;
        if (path.StartsWith("/unauth"))
        {
            code = 401;
            body = "{\"error\": {\"message\": \"invalid key\"}}";
        }
        else if (path.StartsWith("/broken"))
        {
            code = 200;
            body = "<html>not-json</html>";
        }
        else
        {
            code = 200;
            body = "{\"data\": [{\"id\": \"glm-4-flash\"}, {\"id\": \"glm-4-plus\"}]}";
        }

        var payload = Encoding.UTF8.GetBytes(body);
        var head = Encoding.ASCII.GetBytes(
            $"HTTP/1.1 {code} Test\r\nContent-Type: application/json\r\nContent-Length: {payload.Length}\r\nConnection: close\r\n\r\n");
        await stream.WriteAsync(head, ct);
        await stream.WriteAsync(payload, ct);
    }

    private static async Task<string> ReadRequestHeadAsync(NetworkStream stream, CancellationToken ct)
    {
        var buffer = new byte[4096];
        var head = new StringBuilder();
        while (!head.ToString().Contains("\r\n\r\n"))
        {
            var read = await stream.ReadAsync(buffer, ct);
            if (read == 0)
            {
                break;
            }
            head.Append(Encoding.ASCII.GetString(buffer, 0, read));
        }
        return head.ToString();
    }

    private static string? GetHeader(string request, string name)
    {
        foreach (var line in request.Split("\r\n"))
        {
            var idx = line.IndexOf(':');
            if (idx > 0 && line[..idx].Trim().Equals(name, StringComparison.OrdinalIgnoreCase))
            {
                return line[(idx + 1)..].Trim();
            }
        }
        return null;
    }

    public void Dispose()
    {
        _cts.Cancel();
        _listener.Stop();
        _cts.Dispose();
    }
}

public class FetchModelsTests
{
    [Fact]
    public async Task FetchModelsAsync_ReturnsModelIds()
    {
        using var server = new StubModelServer();
        using var client = new PythonSidecarClient();
        var models = await client.FetchModelsAsync($"http://127.0.0.1:{server.Port}/v1", "sk-test");
        Assert.Equal(new[] { "glm-4-flash", "glm-4-plus" }, models);
        Assert.Equal("Bearer sk-test", server.LastAuth);
    }

    [Fact]
    public async Task FetchModelsAsync_Unauthorized_MapsToBusinessError()
    {
        using var server = new StubModelServer();
        using var client = new PythonSidecarClient();
        var failure = await Assert.ThrowsAsync<JsonRpcException>(
            () => client.FetchModelsAsync($"http://127.0.0.1:{server.Port}/unauth", "bad-key"));
        Assert.Equal(-32000, failure.Code);
        Assert.Contains("401", failure.Message);
    }

    [Fact]
    public async Task FetchModelsAsync_ConnectionRefused_MapsToBusinessError()
    {
        using var client = new PythonSidecarClient();
        var failure = await Assert.ThrowsAsync<JsonRpcException>(
            () => client.FetchModelsAsync("http://127.0.0.1:1/v1"));
        Assert.Equal(-32000, failure.Code);
    }
}
