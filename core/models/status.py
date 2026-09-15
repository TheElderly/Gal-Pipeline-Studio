"""翻译条目生命周期状态定义。

状态机主线（正向）::

    RAW → EXTRACTED → TRANSLATED → LQA_PASSED → EXPORTED

返工支线：``TRANSLATED → LQA_FAILED → TRANSLATED``（修复后重新送检）。

状态值混入 ``str`` 落地为全大写字符串，可直接写入 JSON-RPC 报文与
SQLite 文本列，C# 壳层与 Python 核心跨进程边界无需任何转换层。
"""

from enum import Enum


class TranslationStatus(str, Enum):
    """单条文本条目在本地化流水线中的生命周期状态。

    混入 ``str`` 使成员本身就是合法字符串：``TranslationStatus.RAW == "RAW"``
    恒成立，序列化（``json.dumps`` / Pydantic dump）直接输出其字面值。
    """

    RAW = "RAW"                # 原始态：已从封包解出，尚未经适配器解析
    EXTRACTED = "EXTRACTED"    # 已抽取：适配器已解析为中间表示（IR）条目
    TRANSLATED = "TRANSLATED"  # 已翻译：NLP 管线已回填译文，等待质检
    LQA_PASSED = "LQA_PASSED"  # 质检通过：静态守门狗全部断言为绿
    LQA_FAILED = "LQA_FAILED"  # 质检失败：控制符丢失、标点失衡等，需返工
    EXPORTED = "EXPORTED"      # 已导出：译文已回写进目标封包
