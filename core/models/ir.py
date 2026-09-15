"""核心中间表示（Gal-IR）模型 —— 本地化流水线的最高数据契约。

数据流定位（契约先行）::

    适配器 (core/adapters) ──产出──▶ TranslationUnit ──聚合──▶ GalIRProject
                                                            │
    NLP 管线 (core/pipeline) 回填译文、推进状态               │ JSON 序列化跨进程
    LQA 守门狗 (core/lqa) 依据 status 门禁放行/阻断           │ (C# 壳层 ⇄ Python 核心)
    导出器按 raw_text + 标记记录回写目标封包 ◀────────────────┘

层级可变性契约（与 tags.py 的分层）：

* 标记层（AtomicTag / PairedTag）：frozen —— 抽取后不可变的审计凭据；
* 单元层（TranslationUnit）：可变状态载体，但开启 validate_assignment，
  任何字段改写（状态推进、译文回填）都在写入瞬间重跑全部校验；
* 项目层（GalIRProject）：聚合容器，负责 id 全局唯一性与 O(1) 检索。

本文件属技术红线第 1 条界定的核心契约，仅 Agent 0 有权修改。
"""

from collections.abc import Iterator
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    model_validator,
)

from .status import TranslationStatus
from .tags import AtomicTag, PairedTag


def _reject_blank(value: str) -> str:
    """字符串字段统一断言：非空且非纯空白。"""
    if not value.strip():
        raise ValueError("字符串字段不能为空或纯空白")
    return value


NonBlankString = Annotated[str, AfterValidator(_reject_blank)]
"""非空白字符串契约：空串与纯空白串在构造期即被拒绝。

与 tags.NonBlankTag 语义同源、作用域不同：前者约束控制符字面量，
本契约约束 IR 层的标识与正文类字段。二者刻意
各自内聚在自己的契约文件中，后续如需收敛可另开切片合并至共享
primitives 模块。
"""


class TranslationUnit(BaseModel):
    """单条可译文本的中间表示：一条对话、旁白或系统文本即一个单元。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: NonBlankString = Field(
        ...,
        description="全局唯一标识（建议 脚本名-序号 形式，如 scn01-000042）",
    )
    speaker: NonBlankString | None = Field(
        default=None,
        description="角色名；None 表示旁白/系统文本，存在时必须非空白",
    )
    raw_text: NonBlankString = Field(
        ...,
        description="包含控制符的原始文本，回写封包时的唯一权威来源",
    )
    extracted_text: NonBlankString = Field(
        ...,
        description="剥离控制符后的纯可译正文；为空即不构成翻译单元，构造期拒绝",
    )
    atomic_tags: list[AtomicTag] = Field(
        default_factory=list,
        description="原子控制符清单（frozen 审计凭据，按 position 回填）",
    )
    paired_tags: list[PairedTag] = Field(
        default_factory=list,
        description="成对控制符清单（frozen 审计凭据，开闭标记逐字节复写）",
    )
    translated_text: NonBlankString | None = Field(
        default=None,
        description="译文；None 表示尚未翻译，一旦填写必须非空白",
    )
    status: TranslationStatus = Field(
        default=TranslationStatus.RAW,
        description="生命周期状态，由各阶段引擎推进",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="引擎特有上下文（如 PSB 条目偏移、HG3 伴生坐标）",
    )


class GalIRProject(BaseModel):
    """Gal-IR 顶层容器：一次本地化作业的完整单元集合与项目元信息。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    project_name: NonBlankString = Field(..., description="项目名称")
    source_lang: NonBlankString = Field(..., description="源语言 BCP 47 代码，如 ja")
    target_lang: NonBlankString = Field(..., description="目标语言 BCP 47 代码，如 zh-CN")
    engine_type: NonBlankString = Field(..., description="目标引擎类型，如 psb / hg3")

    units: list[TranslationUnit] = Field(
        default_factory=list,
        description="全部翻译单元；id 在本列表内必须全局唯一",
    )

    _index: dict[str, TranslationUnit] = PrivateAttr(default_factory=dict)
    """id → 单元实例的私有检索索引，仅由 _sync_index 整体重建，绝不就地修改。"""

    @model_validator(mode="after")
    def _validate_unique_and_sync_index(self) -> "GalIRProject":
        """构造期与整体重赋值期：校验 id 全局唯一并重建检索索引。

        依赖 validate_assignment：对 units 的整体重赋值会重跑本校验器，
        因此「换入重复 id 的单元列表」在赋值瞬间即被拦截。
        """
        self._sync_index()
        return self

    def _sync_index(self) -> None:
        """重建索引；发现重复 id 立即抛错，拒绝静默服务脏状态。"""
        index: dict[str, TranslationUnit] = {}
        for unit in self.units:
            if unit.id in index:
                raise ValueError(
                    f"TranslationUnit id 重复: {unit.id!r}"
                    "（同一 GalIRProject 内 id 必须全局唯一）"
                )
            index[unit.id] = unit
        self._index = index

    def _ensure_index_fresh(self) -> dict[str, TranslationUnit]:
        """读取前确保索引与 units 同步。

        units 的整体重赋值会触发校验器自动重建索引；直接对 units 执行
        append / remove 会绕过校验，此处凭长度漂移自愈。按下标等长就地
        替换单元无法被检测，属未定义行为——结构性修改的契约是：要么
        整体重赋值 units，要么重新构造容器。
        """
        if len(self._index) != len(self.units):
            self._sync_index()
        return self._index

    def get_unit(self, unit_id: str) -> TranslationUnit:
        """按 id O(1) 检索翻译单元；未命中抛 KeyError。"""
        try:
            return self._ensure_index_fresh()[unit_id]
        except KeyError:
            raise KeyError(
                f"项目中不存在 id 为 {unit_id!r} 的 TranslationUnit"
            ) from None

    def __len__(self) -> int:
        return len(self.units)

    def __iter__(self) -> Iterator[TranslationUnit]:
        return iter(self.units)

    def __contains__(self, item: object) -> bool:
        """支持 ``"unit-id" in project`` 与 ``unit in project`` 两种判断。"""
        index = self._ensure_index_fresh()
        if isinstance(item, TranslationUnit):
            return index.get(item.id) is item
        if isinstance(item, str):
            return item in index
        return False
