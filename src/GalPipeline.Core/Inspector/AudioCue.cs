using System.Text.Json;

namespace GalPipeline.Core.Inspector;

/// <summary>
/// 单元的语音线索（从 <c>metadata</c> 解析）。零依赖的纯数据载体 ——
/// 解析逻辑放在共享库里，壳层 VM、IPC 客户端与自动化测试消费同一份契约。
///
/// 元数据形态（两种都接受）::
///
///     "audio": "vo_00012.ogg"                                  // 仅资源名
///     "audio": { "file": "vo_00012.ogg",
///                "duration_ms": 1850, "comp_ms": 200 }         // 完整线索
///
/// 顶层键按 ``audio`` → ``voice`` 顺序查找，方便适配器沿用引擎原生叫法；
/// 对象内的键同时接受 snake_case 与 camelCase（Python 侧与 C# 侧各自顺手）。
/// </summary>
public sealed record AudioCue(string? File, int? DurationMs, int? CompMs)
{
    /// <summary>顶层元数据键的查找顺序（先 audio 后 voice）。</summary>
    private static readonly string[] MetadataKeys = ["audio", "voice"];

    private static readonly string[] FileKeys = ["file", "path", "name", "storage", "src"];
    private static readonly string[] DurationKeys = ["duration_ms", "durationMs", "duration"];
    private static readonly string[] CompKeys = ["comp_ms", "compMs", "comp"];

    /// <summary>是否指向一个具名音频资源（决定能否真正播放）。</summary>
    public bool HasAsset => !string.IsNullOrWhiteSpace(File);

    /// <summary>
    /// 从单元 metadata 解析语音线索；无任何可识别字段时返回 <c>null</c>（即「该行无配音」）。
    ///
    /// 判定口径：只要能取到**任一**已识别字段（资源名非空白 / duration_ms / comp_ms）
    /// 就算存在音频。取不到任何字段即视为无音频 —— 与「适配器没写 metadata」这一
    /// 最常见情形同路，故不会把缺失数据误判成静音。
    /// </summary>
    public static AudioCue? FromMetadata(IReadOnlyDictionary<string, JsonElement>? metadata)
    {
        if (metadata is null)
        {
            return null;
        }
        foreach (var key in MetadataKeys)
        {
            if (!metadata.TryGetValue(key, out var value))
            {
                continue;
            }
            var cue = FromValue(value);
            if (cue is not null)
            {
                return cue;
            }
        }
        return null;
    }

    private static AudioCue? FromValue(JsonElement value) => value.ValueKind switch
    {
        // 简写形态："audio": "vo_00012.ogg"
        JsonValueKind.String => Blank(value.GetString()) is { } file ? new AudioCue(file, null, null) : null,
        JsonValueKind.Object => FromObject(value),
        _ => null,
    };

    private static AudioCue? FromObject(JsonElement value)
    {
        var file = ReadString(value, FileKeys);
        var duration = ReadInt(value, DurationKeys);
        var comp = ReadInt(value, CompKeys);
        if (file is null && duration is null && comp is null)
        {
            return null; // 空对象 / 全是无法识别的键：不构成语音线索
        }
        return new AudioCue(file, duration, comp);
    }

    private static string? ReadString(JsonElement value, string[] keys)
    {
        foreach (var key in keys)
        {
            if (value.TryGetProperty(key, out var element) && element.ValueKind is JsonValueKind.String)
            {
                if (Blank(element.GetString()) is { } text)
                {
                    return text;
                }
            }
        }
        return null;
    }

    private static int? ReadInt(JsonElement value, string[] keys)
    {
        foreach (var key in keys)
        {
            if (!value.TryGetProperty(key, out var element))
            {
                continue;
            }
            if (element.ValueKind is JsonValueKind.Number && element.TryGetInt32(out var number))
            {
                return number;
            }
            // 数字以字符串形式落盘的情况（部分封包解析器会这样产出）
            if (element.ValueKind is JsonValueKind.String
                && int.TryParse(element.GetString(), out var parsed))
            {
                return parsed;
            }
        }
        return null;
    }

    private static string? Blank(string? text)
        => string.IsNullOrWhiteSpace(text) ? null : text.Trim();
}
