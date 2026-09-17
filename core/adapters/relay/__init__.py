"""通用中继适配器包 —— 社区工具中间文本 ↔ Gal-IR 的标准接缝。

三套适配器只懂**中间文本格式**，引擎能力边界（dump/回编译工具、
补丁形态）由 ``recipes`` 声明 —— 格式与能力正交。全部实现
``core.adapters.base.BaseEngineAdapter``，经 ``_ADAPTER_FACTORIES``
注册（键 = GalIRProject.engine_type），对 RPC/壳层零特例。
"""

from .json_relay import JsonAdapter
from .marked import MarkedTextAdapter
from .recipes import (
    PATCH_LOOSE,
    PATCH_OVERLAY,
    PATCH_XDELTA,
    RECIPES,
    RelayCapabilityError,
    RelayRecipe,
    get_recipe,
)
from .tabular import CSVTabularAdapter, TabularAdapter, TabularFormat

__all__ = [
    "CSVTabularAdapter",
    "JsonAdapter",
    "MarkedTextAdapter",
    "PATCH_LOOSE",
    "PATCH_OVERLAY",
    "PATCH_XDELTA",
    "RECIPES",
    "RelayCapabilityError",
    "RelayRecipe",
    "TabularAdapter",
    "TabularFormat",
    "get_recipe",
]
