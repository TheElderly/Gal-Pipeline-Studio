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
