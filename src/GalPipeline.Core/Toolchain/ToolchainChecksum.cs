using System.Security.Cryptography;

namespace GalPipeline.Core.Toolchain;

/// <summary>SHA-256 完整性校验工具（下载器与测试桩共用同一份实现）。</summary>
public static class ToolchainChecksum
{
    /// <summary>计算文件 SHA-256（小写十六进制）。</summary>
    public static async Task<string> ComputeSha256Async(
        string filePath, CancellationToken cancellationToken = default)
    {
        await using var stream = File.OpenRead(filePath);
        var hash = await SHA256.HashDataAsync(stream, cancellationToken).ConfigureAwait(false);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    /// <summary>
    /// 校验文件 SHA-256 与期望值一致；不符抛 <see cref="ToolchainException"/>
    /// （下错/被篡改的压缩包绝不能进入解包与安装）。
    /// </summary>
    public static async Task VerifySha256Async(
        string filePath, string expectedSha256, CancellationToken cancellationToken = default)
    {
        var actual = await ComputeSha256Async(filePath, cancellationToken).ConfigureAwait(false);
        var expected = expectedSha256.Trim().ToLowerInvariant();
        if (actual != expected)
        {
            throw new ToolchainException(
                $"SHA-256 完整性校验失败：期望 {expected}，实际 {actual} —— "
                + "压缩包可能下载不完整或被篡改，已拒绝进入安装流程");
        }
    }
}
