namespace GalPipeline.Core.Inspector;

/// <summary>
/// 波形采样序列生成器：**确定性的**柱高包络（同 seed 恒得同一条波形）。
///
/// 为什么不解码真实音频：本项目的音频资产是引擎私有格式，解码属
/// 适配器层的多媒体旁路（离线红线内），而 Inspector 需要的只是一条
/// 「看得出这段语音的节奏」的示意包络。因此这里合成一条与真实振幅无关、
/// 但**跨会话稳定可复现**的包络 —— 同一行每次选中都长一样，
/// 不会出现「点一下变一个样」的廉价抖动。
///
/// 真实波形就位后的替换点：把 <see cref="ForVoice"/> 换成从适配器解码出的
/// 振幅数组即可，调用方（Inspector 状态派生）签名不变。
/// </summary>
public static class WaveformSampler
{
    /// <summary>柱子数量（与 XAML 的中轴对称细波形渲染配合）。</summary>
    public const int BarCount = 32;

    /// <summary>静音基线高度：2px，正好读出中轴线上的一条平直细线。</summary>
    public const int SilenceHeight = 2;

    private const int MinHeight = 12;
    private const int MaxHeight = 52;

    /// <summary>无配音时的平直静音基线（恒定值，不随单元变化）。</summary>
    public static IReadOnlyList<int> Silence()
        => Enumerable.Repeat(SilenceHeight, BarCount).ToArray();

    /// <summary>
    /// 有配音时的确定性包络：中段饱满、首尾按 <c>sin</c> 包络收细，
    /// 叠加以 seed 为源的固定伪随机扰动，令每行波形彼此不同但各自稳定。
    /// </summary>
    public static IReadOnlyList<int> ForVoice(string? seed)
    {
        var rng = new Random(StableSeed(seed));
        var bars = new int[BarCount];
        for (var i = 0; i < BarCount; i++)
        {
            var t = (double)i / (BarCount - 1);       // 0 → 1
            var envelope = 0.25 + 0.75 * Math.Sin(Math.PI * t);
            bars[i] = (int)Math.Round(rng.Next(MinHeight, MaxHeight) * envelope);
        }
        return bars;
    }

    /// <summary>
    /// 稳定种子：<c>string.GetHashCode()</c> 在每个进程随机化（.NET Core 起），
    /// 直接拿来当种子会让波形「重启一次变一个样」，故手写确定性折叠。
    /// 未检出溢出是刻意依赖 —— 溢出回绕在 C# 里是确定的，正好当哈希用。
    /// </summary>
    public static int StableSeed(string? seed)
    {
        var value = 17;
        if (!string.IsNullOrEmpty(seed))
        {
            foreach (var ch in seed)
            {
                value = unchecked(value * 31 + ch);
            }
        }
        return value;
    }
}
