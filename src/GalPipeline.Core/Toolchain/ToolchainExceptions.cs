namespace GalPipeline.Core.Toolchain;

/// <summary>工具链域异常基类：探测失败、校验不符等。</summary>
public class ToolchainException : Exception
{
    public ToolchainException(string message) : base(message)
    {
    }
}

/// <summary>
/// 工具缺失异常 —— 消费方（如 FFmpegAudioCache）在解析结果为 Missing 时
/// 抛出，**携带完整装配引导**（工具箱落位 / PATH 安装 / 官方下载三选一），
/// 绝不允许「静默崩溃」或「无声失败」。壳层直接把 Message 展示给用户即可行动。
/// </summary>
public sealed class ToolchainMissingException : ToolchainException
{
    public ToolchainMissingException(ExternalToolInfo toolInfo, string? detail = null)
        : base(BuildGuidance(toolInfo, detail))
    {
        ToolInfo = toolInfo;
        ToolType = toolInfo.Type;
    }

    public ToolType ToolType { get; }

    public ExternalToolInfo ToolInfo { get; }

    private static string BuildGuidance(ExternalToolInfo info, string? detail)
    {
        var lines = new List<string>
        {
            $"未找到外部工具：{info.DisplayName}（{info.Type}）",
            $"装配方式一（推荐）：把 {info.ExecutableName} 放入程序目录下的 {info.ToolboxRelativePath}",
            "装配方式二：正常安装该工具并确保其目录在 PATH 环境变量中",
            $"官方获取：{info.DownloadUrl}",
        };
        if (detail is not null)
        {
            lines.Insert(1, $"详情：{detail}");
        }
        return string.Join(Environment.NewLine, lines);
    }
}
