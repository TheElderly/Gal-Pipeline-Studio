"""适配器抽象基类 —— 引擎特异性与流水线通用性之间的唯一接缝。

本文件属技术红线第 1 条界定的核心契约（与 core/models/ir.py 同级），
仅 Agent 0 有权修改；具体引擎实现（PSB / HG3 / ...）由 Agent 1 在
core/adapters/ 下派生本类完成。

标准作业生命周期::

    detect(file) ──True──▶ extract_to_ir(file) ──▶ GalIRProject
                                                    │ NLP 管线翻译 + LQA 门禁
    ir_to_asset(project, out_dir) ◀────────────────┘
    normalize_image(raw_bytes)     正交旁路：私有格式/BMP → RGBA PNG-32

实现方规范性约束（MUST / MUST NOT）：

* detect 必须基于魔数/特征字节判定，严禁仅凭文件后缀放行；
* extract_to_ir 产出的单元必须满足 Gal-IR 全部构造契约
  （id 全局唯一、extracted_text 非空、控制符以冻结标记记录），
  且交付时处于 EXTRACTED 状态；
* ir_to_asset 回写必须逐字节复写标记记录（控制符守恒）；
* normalize_image 必须全程内存作业，严禁落临时文件、严禁任何网络外发
  （技术红线第 2 条：多媒体资产 100% 本地离线）；
* 外部工具（FreeMote / FFmpeg 等）只能以独立进程 CLI 跨进程调用
  （技术红线第 3 条：GPL 物理隔离）；
* 一切实现层错误必须派生自 EngineAdapterError，供 JSON-RPC
  错误映射层统一识别与分级。
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..models.ir import GalIRProject


class EngineAdapterError(Exception):
    """适配器层专有异常基类。

    引擎实现（含其包装的外部 CLI 工具失败）抛出的一切错误都必须
    派生自本类而非裸 Exception，保证 JSON-RPC 错误映射层能用一句
    ``except EngineAdapterError`` 一网打尽并统一分级上报。
    """


class UnsupportedFormatError(EngineAdapterError):
    """目标文件不属于本适配器管辖格式（魔数/特征字节不匹配）。

    典型抛出点：detect 返回 False 后调用方仍强制解析，或文件头
    校验在解包中途失败。
    """


class BaseEngineAdapter(ABC):
    """游戏引擎适配器抽象基类：一个目标引擎格式对应一个实现类。

    detect / extract_to_ir / ir_to_asset / normalize_image 四个抽象
    方法构成引擎特异性代码的完整边界——本类之外，流水线其余部分
    对具体引擎格式保持完全无知。
    """

    @abstractmethod
    def detect(self, file_path: Path) -> bool:
        """判断目标文件是否属于本适配器管辖格式。

        必须通过魔数/特征字节（如 PSB 头、HG3 签名）判定，严禁仅凭
        文件后缀名放行——后缀在玩家环境中并不可信。返回 True 即声明
        本适配器对后续解析与回写负全责。
        """

    @abstractmethod
    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        """解包并解析游戏资产，产出符合 Gal-IR 规范的内存项目容器。

        交付物必须满足 Gal-IR 全部构造契约：单元 id 全局唯一、
        extracted_text 非空、全部控制符以冻结标记（AtomicTag /
        PairedTag）记录；单元状态置为 EXTRACTED；引擎特有上下文
        （条目偏移、伴生坐标等）写入 metadata。
        """

    @abstractmethod
    def ir_to_asset(self, project: GalIRProject, output_dir: Path) -> Path:
        """将翻译/质检完成的 IR 项目反向回写为目标引擎文件。

        回写必须依据 raw_text 与冻结标记逐字节复写控制符（控制符
        守恒）；输出写入 output_dir，返回值是实际写成的目标文件路径。
        """

    @abstractmethod
    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        """通用图像归一化：私有格式/BMP 原始字节 → RGBA PNG-32 字节。

        必须全程内存作业——严禁产生未清理的本地磁盘临时文件，严禁
        向任何网络端点外发图像数据（离线安全红线）。kwargs 承载
        引擎特有选项（如调色板处理策略）。
        """
