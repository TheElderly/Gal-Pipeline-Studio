"""控制符标记模型（Pydantic v2）。

Galgame 脚本文本中混嵌大量不可译控制符，按结构分为两类：

* 原子型（``AtomicTag``）：不可再分、无包裹语义的单一标记，
  如换行 ``\\n``、格式化占位 ``%d``、注音 ``[ruby text=よみ]``；
* 成对型（``PairedTag``）：开标记 + 被包裹文本 + 闭标记的复合结构，
  如 ``<color=0xFF0000>绯色</color>``。

抽取阶段将它们从可译文本中剥离暂存，翻译完成后按记录逐字节复写回填。
本模块是 LQA 守门狗「控制符守恒」断言的数据基石：标记一经构造即冻结
（frozen）并拒绝未知字段，杜绝流水线中途被静默篡改。
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _reject_blank(value: str) -> str:
    """tag 字段统一断言：非空且非纯空白。

    空串与纯空白串（空格、制表符、裸换行符等）都不构成可识别的
    控制符标记——真实的换行与空格属于正文文本，由 IR 条目承载，
    而非标记注册表。
    """
    if not value.strip():
        raise ValueError("tag 字段不能为空或纯空白")
    return value


NonBlankTag = Annotated[str, AfterValidator(_reject_blank)]
"""非空白标记字符串契约：空串与纯空白串在构造期即被拒绝。"""


class AtomicTag(BaseModel):
    """原子控制符：无包裹语义、不可再分的单一标记。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag_id: NonBlankTag = Field(
        ...,
        description="标记类型标识（由适配器抽取器赋予），如 newline、format_int、ruby",
    )
    raw_tag: NonBlankTag = Field(
        ...,
        description="原文中的字面形式；回填时必须逐字节复写，禁止任何改写",
    )
    position: int = Field(
        ...,
        ge=0,
        description="该标记在原始文本中的字符偏移量（从 0 计）",
    )


class PairedTag(BaseModel):
    """成对控制符：开标记、被包裹文本、闭标记三段构成的复合结构。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_tag: NonBlankTag = Field(
        ...,
        description="开标记字面形式，如 <color=0xFF0000>",
    )
    end_tag: NonBlankTag = Field(
        ...,
        description="闭标记字面形式，如 </color>",
    )
    inner_text: str = Field(
        ...,
        description="被包裹的内部文本，翻译的语义载体，允许为空串",
    )
