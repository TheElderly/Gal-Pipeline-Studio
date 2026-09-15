"""core/adapters/base.py 契约回归测试（Batch 1.4 最小验证固化）。

覆盖范围与切片承诺一一对应：

1. 抽象屏障：BaseEngineAdapter 不可直接实例化，__abstractmethods__
   包含且仅包含四个抽象方法；
2. 残缺子类拦截：缺少任意一个抽象方法即被 TypeError 拦截，
   且报错精确点名缺失成员；
3. 异常分类学：UnsupportedFormatError 继承 EngineAdapterError，
   可被基类统一捕获，层级方向不可倒置；
4. 完整子类契约：四方法齐全的桩子类正常实例化，入参/返回契约
   可履行（含魔数判定示范与 kwargs 透传）；
5. 签名钉死：inspect.signature 验证四个抽象方法的形参名、类型
   标注与返回值标注——签名即契约，改动必须显式过审。
"""

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.base import (
    BaseEngineAdapter,
    EngineAdapterError,
    UnsupportedFormatError,
)
from core.models.ir import GalIRProject

ABSTRACT_METHODS = frozenset(
    {"detect", "extract_to_ir", "ir_to_asset", "normalize_image"}
)

SIGNATURE_CONTRACT = {
    "detect": ([("file_path", Path)], bool),
    "extract_to_ir": ([("file_path", Path)], GalIRProject),
    "ir_to_asset": ([("project", GalIRProject), ("output_dir", Path)], Path),
    "normalize_image": ([("raw_bytes", bytes)], bytes),
}

PSB_MAGIC = b"PSB\x00"


class CompleteStubAdapter(BaseEngineAdapter):
    """四方法齐全的最小桩实现：仅用于契约验证，不承载任何引擎逻辑。

    detect 特意按魔数判定——示范基类契约要求的方式，而非后缀放行。
    """

    def detect(self, file_path: Path) -> bool:
        try:
            with file_path.open("rb") as fh:
                return fh.read(4) == PSB_MAGIC
        except OSError:
            return False

    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        return GalIRProject(
            project_name="桩工程",
            source_lang="ja",
            target_lang="zh-CN",
            engine_type="psb",
        )

    def ir_to_asset(self, project: GalIRProject, output_dir: Path) -> Path:
        return output_dir / f"{project.project_name}.psb"

    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + raw_bytes + str(sorted(kwargs)).encode("ascii")


def _make_partial_cls(omit: str) -> type[BaseEngineAdapter]:
    """动态构造恰好只缺 omit 一个抽象方法的残缺子类。"""

    def detect(self, file_path: Path) -> bool:
        return True

    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        return CompleteStubAdapter().extract_to_ir(file_path)

    def ir_to_asset(self, project: GalIRProject, output_dir: Path) -> Path:
        return output_dir / "out.psb"

    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    members: dict = {
        "detect": detect,
        "extract_to_ir": extract_to_ir,
        "ir_to_asset": ir_to_asset,
        "normalize_image": normalize_image,
    }
    members.pop(omit)
    return type(f"PartialMissing{omit.capitalize()}", (BaseEngineAdapter,), members)


@pytest.fixture
def adapter() -> CompleteStubAdapter:
    return CompleteStubAdapter()


# ---------------------------------------------------------------------------
# 1. 抽象屏障
# ---------------------------------------------------------------------------


class TestAbstractBarrier:
    """抽象基类本体：不可实例化，抽象登记恰好四项。"""

    def test_direct_instantiation_blocked(self):
        with pytest.raises(TypeError, match="abstract class"):
            BaseEngineAdapter()

    def test_abstractmethods_exactly_four(self):
        assert BaseEngineAdapter.__abstractmethods__ == ABSTRACT_METHODS
        assert len(ABSTRACT_METHODS) == 4


# ---------------------------------------------------------------------------
# 2. 残缺子类拦截
# ---------------------------------------------------------------------------


class TestPartialSubclassInterception:
    """缺一即拦截：报错必须精确点名缺失成员，不许含糊。"""

    @pytest.mark.parametrize("omitted", sorted(ABSTRACT_METHODS))
    def test_single_missing_method_blocks_instantiation(self, omitted):
        partial_cls = _make_partial_cls(omit=omitted)
        assert partial_cls.__abstractmethods__ == frozenset({omitted})
        with pytest.raises(TypeError, match=omitted):
            partial_cls()

    def test_empty_subclass_reports_all_four(self):
        empty_cls = type("EmptyAdapter", (BaseEngineAdapter,), {})
        assert empty_cls.__abstractmethods__ == ABSTRACT_METHODS
        with pytest.raises(TypeError):
            empty_cls()


# ---------------------------------------------------------------------------
# 3. 异常分类学
# ---------------------------------------------------------------------------


class TestExceptionTaxonomy:
    """异常树：派生关系、统一捕获口与方向性。"""

    def test_inheritance_chain(self):
        assert issubclass(UnsupportedFormatError, EngineAdapterError)
        assert issubclass(EngineAdapterError, Exception)
        assert not issubclass(EngineAdapterError, UnsupportedFormatError)  # 方向不可倒置

    def test_derived_caught_by_base(self):
        with pytest.raises(EngineAdapterError):
            raise UnsupportedFormatError("魔数不匹配: 期望 'PSB'，实际 0x00000000")

    def test_message_carries_context(self):
        err = UnsupportedFormatError("魔数不匹配: 期望 'PSB'，实际 0x00000000")
        assert "PSB" in str(err)


# ---------------------------------------------------------------------------
# 4. 完整子类契约
# ---------------------------------------------------------------------------


class TestCompleteStubContract:
    """四方法齐全的子类：实例化畅通，入参/返回契约可履行。"""

    def test_instantiation_and_isinstance(self, adapter):
        assert isinstance(adapter, BaseEngineAdapter)

    def test_detect_magic_number_not_extension(self, adapter, tmp_path):
        """同后缀不同魔数必须拒绝——后缀在玩家环境不可信。"""
        genuine = tmp_path / "scene.psb"
        genuine.write_bytes(PSB_MAGIC + b"payload")
        fake = tmp_path / "fake.psb"
        fake.write_bytes(b"\x00\x00\x00\x00")
        assert adapter.detect(genuine) is True
        assert adapter.detect(fake) is False
        assert adapter.detect(tmp_path / "ghost.psb") is False  # 不存在不抛错

    def test_extract_to_ir_returns_galir_project(self, adapter, tmp_path):
        genuine = tmp_path / "scene.psb"
        genuine.write_bytes(PSB_MAGIC)
        project = adapter.extract_to_ir(genuine)
        assert isinstance(project, GalIRProject)
        assert project.engine_type == "psb"
        assert len(project) == 0  # 桩不产出单元；容器行为由 test_ir_models 保障

    def test_ir_to_asset_returns_path_under_output_dir(self, adapter, tmp_path):
        project = adapter.extract_to_ir(tmp_path / "scene.psb")
        output_dir = tmp_path / "out"
        result = adapter.ir_to_asset(project, output_dir)
        assert isinstance(result, Path)
        assert result.parent == output_dir

    def test_normalize_image_in_memory_bytes_contract(self, adapter):
        raw = b"\x00\x01\x02\xff"
        out = adapter.normalize_image(raw, palette="keep")
        assert isinstance(out, bytes)
        assert out.startswith(b"\x89PNG\r\n\x1a\n")
        assert raw in out  # 入参字节被完整消费


# ---------------------------------------------------------------------------
# 5. 签名钉死
# ---------------------------------------------------------------------------


class TestSignaturePinning:
    """签名即契约：形参名、类型标注、返回标注逐一钉死。"""

    @pytest.mark.parametrize("method_name", sorted(SIGNATURE_CONTRACT))
    def test_signature_matches_contract(self, method_name):
        func = getattr(BaseEngineAdapter, method_name)
        sig = inspect.signature(func)
        expected_params, expected_return = SIGNATURE_CONTRACT[method_name]

        assert list(sig.parameters)[0] == "self"
        positional = [
            (name, param.annotation)
            for name, param in sig.parameters.items()
            if name != "self" and param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        ]
        assert positional == expected_params
        assert sig.return_annotation is expected_return

    def test_normalize_image_accepts_var_keyword_options(self):
        sig = inspect.signature(BaseEngineAdapter.normalize_image)
        kwarg = sig.parameters["kwargs"]
        assert kwarg.kind is inspect.Parameter.VAR_KEYWORD
        assert kwarg.annotation is Any
