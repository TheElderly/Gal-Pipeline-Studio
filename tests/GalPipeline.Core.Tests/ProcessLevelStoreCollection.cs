using Xunit;

namespace GalPipeline.Core.Tests;

/// <summary>
/// 操作进程级单例状态（TranslationSettingsStore / ToolchainRuntime）的
/// 测试必须同串行 —— 默认按类并行会让两组用例互相踩脏彼此的基准快照。
/// </summary>
[CollectionDefinition(nameof(ProcessLevelStoreCollection))]
public sealed class ProcessLevelStoreCollection;
