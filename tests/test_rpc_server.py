"""core/server/rpc.py 契约回归测试 —— JSON-RPC 2.0 协议与业务方法往返。

覆盖范围：

1. 协议层：ping 探活、非法 JSON（-32700）、缺字段/错版本/批量数组
   （-32600）、未知方法（-32601）、参数违约（-32602）、通知静默；
2. 业务层：detect_format / extract_to_ir / translate_batch（Mock 注入）/
   ir_to_asset 的完整 JSON-RPC 消息往返；
3. 异常映射：-32000（UnsupportedFormatError）、-32602（文件不存在、
   参数校验失败）、-32603（未捕获异常兜底）；
4. serve_stdio 行式循环：空白行跳过、通知静默、EOF 自然退出。
"""

import io
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.server.rpc as rpc_module
from core.models.status import TranslationStatus
from core.server.rpc import build_default_dispatcher, serve_stdio

KAG_SAMPLE = (
    "; 标题注释\n"
    "*begin\n"
    "【ヒロイン】おはよう[ruby text=\"はよ\"]\n"
)


def _rpc(method: str, params: dict | None = None, req_id: int | str | None = 1) -> str:
    frame: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        frame["params"] = params
    if req_id is not None:
        frame["id"] = req_id
    return json.dumps(frame, ensure_ascii=False)


@pytest.fixture
def dispatcher():
    return build_default_dispatcher()


@pytest.fixture
def ks_file(tmp_path: Path) -> Path:
    target = tmp_path / "scene.ks"
    target.write_text(KAG_SAMPLE, encoding="utf-8")
    return target


def _result(dispatcher, raw: str) -> dict:
    return json.loads(dispatcher.handle_request(raw))


# ---------------------------------------------------------------------------
# 1. 协议层
# ---------------------------------------------------------------------------


class TestProtocol:
    """探活与帧校验：错误码与 id 回显语义。"""

    def test_ping_roundtrip(self, dispatcher):
        resp = _result(dispatcher, _rpc("ping"))
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": "pong"}

    def test_parse_error_returns_null_id(self, dispatcher):
        resp = _result(dispatcher, "{not-json")
        assert resp["error"]["code"] == -32700 and resp["id"] is None

    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            ('{"method": "ping", "id": 1}', "缺jsonrpc"),
            ('{"jsonrpc": "1.0", "method": "ping", "id": 1}', "错版本"),
            ('{"jsonrpc": "2.0", "id": 1}', "缺method"),
            ('{"jsonrpc": "2.0", "method": 42, "id": 1}', "method非字符串"),
            ('[{"jsonrpc": "2.0", "method": "ping", "id": 1}]', "批量数组"),
        ],
        ids=["缺jsonrpc", "错版本", "缺method", "method非字符串", "批量数组"],
    )
    def test_invalid_request(self, dispatcher, raw, why):
        resp = _result(dispatcher, raw)
        assert resp["error"]["code"] == -32600
        assert resp["jsonrpc"] == "2.0"

    def test_unknown_method(self, dispatcher):
        resp = _result(dispatcher, _rpc("no_such_method"))
        assert resp["error"]["code"] == -32601
        assert "no_such_method" in resp["error"]["message"]

    def test_invalid_params_missing_argument(self, dispatcher):
        resp = _result(dispatcher, _rpc("detect_format", {}))
        assert resp["error"]["code"] == -32602

    def test_notification_is_silent_even_on_error(self, dispatcher):
        assert dispatcher.handle_request('{"jsonrpc": "2.0", "method": "ping"}') == ""
        assert dispatcher.handle_request('{"jsonrpc": "2.0", "method": "ghost"}') == ""


# ---------------------------------------------------------------------------
# 2. 业务层往返
# ---------------------------------------------------------------------------


class TestBusinessMethods:
    """四类业务方法的完整消息往返。"""

    def test_detect_format(self, dispatcher, ks_file, tmp_path):
        hit = _result(dispatcher, _rpc("detect_format", {"file_path": str(ks_file)}))["result"]
        assert hit == {"detected": True, "adapter": "kag"}
        plain = tmp_path / "plain.ks"
        plain.write_bytes(b"plain text only\n")
        miss = _result(dispatcher, _rpc("detect_format", {"file_path": str(plain)}))["result"]
        assert miss == {"detected": False, "adapter": ""}

    def test_extract_to_ir_roundtrip(self, dispatcher, ks_file):
        envelope = _result(dispatcher, _rpc("extract_to_ir", {"file_path": str(ks_file)}))["result"]
        assert envelope["project_id"] == "scene"
        assert envelope["unit_count"] == 1
        assert envelope["stats"]["by_status"] == {"EXTRACTED": 1}
        assert envelope["project"]["engine_type"] == "kag"
        unit = envelope["project"]["units"][0]
        assert unit["speaker"] == "ヒロイン"
        assert unit["status"] == "EXTRACTED"
        assert unit["metadata"]["kag_source"] == KAG_SAMPLE  # 回写链路依赖存源

    def test_translate_batch_with_mock(self, dispatcher, ks_file, monkeypatch):
        project = _result(dispatcher, _rpc("extract_to_ir", {"file_path": str(ks_file)}))["result"]["project"]

        class MockTranslator:
            def __init__(self, config):
                pass

            def translate_batch(self, units, context=None):
                for u in units:
                    u.translated_text = f"mock译文[{u.id}]"
                    u.status = TranslationStatus.LQA_PASSED
                return units

        monkeypatch.setattr(rpc_module, "GalgameTranslator", MockTranslator)
        resp = _result(
            dispatcher,
            _rpc(
                "translate_batch",
                {
                    "units": project["units"],
                    "config": {"api_base": "http://mock/v1", "model_name": "glm-test"},
                },
            ),
        )["result"]
        # 契约：批次结果携带 units + usage 两个键（mock 无 last_usage 时 usage 零值兜底）
        assert set(resp) == {"units", "usage"}
        assert resp["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        assert resp["units"][0]["status"] == "LQA_PASSED"
        assert resp["units"][0]["translated_text"] == "mock译文[scene-00003]"

    def test_ir_to_asset_roundtrip(self, dispatcher, ks_file, tmp_path):
        project = _result(dispatcher, _rpc("extract_to_ir", {"file_path": str(ks_file)}))["result"]["project"]
        out_dir = tmp_path / "out"
        resp = _result(
            dispatcher,
            _rpc(
                "ir_to_asset",
                {"project": project, "output_dir": str(out_dir)},
            ),
        )["result"]
        output = Path(resp["output_path"])
        assert output.exists()
        assert output.read_bytes().startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
        assert "*begin" in output.read_text(encoding="utf-8-sig")  # 骨架保真


# ---------------------------------------------------------------------------
# 3. 异常映射
# ---------------------------------------------------------------------------


class TestExceptionMapping:
    """业务异常 → 规范错误码的唯一映射点。"""

    def test_unsupported_format_maps_to_business_error(self, dispatcher, tmp_path):
        garbage = tmp_path / "garbage.ks"
        garbage.write_bytes(b"no kag features here\n")
        resp = _result(dispatcher, _rpc("extract_to_ir", {"file_path": str(garbage)}))
        assert resp["error"]["code"] == -32000
        assert "认领" in resp["error"]["message"]

    def test_missing_file_maps_to_business_error(self, dispatcher, tmp_path):
        """缺失文件被 detect 防御性拒认领 → 统一落业务错误码。"""
        ghost = tmp_path / "ghost.ks"
        resp = _result(dispatcher, _rpc("extract_to_ir", {"file_path": str(ghost)}))
        assert resp["error"]["code"] == -32000
        assert "认领" in resp["error"]["message"]

    def test_unknown_engine_type_maps_to_business_error(self, dispatcher, tmp_path):
        resp = _result(
            dispatcher,
            _rpc("ir_to_asset", {"project": {"project_name": "x", "source_lang": "ja",
                                             "target_lang": "zh-CN", "engine_type": "unknown"},
                                 "output_dir": str(tmp_path)}),
        )
        assert resp["error"]["code"] == -32000

    def test_uncaught_exception_falls_back_to_internal_error(self, dispatcher):
        def boom():
            raise RuntimeError("意外爆炸")

        dispatcher.register("boom", boom)
        resp = _result(dispatcher, _rpc("boom"))
        assert resp["error"]["code"] == -32603
        assert "RuntimeError" in resp["error"]["message"]


# ---------------------------------------------------------------------------
# 4. serve_stdio 行式循环
# ---------------------------------------------------------------------------


class TestServeStdio:
    """行式主循环：空白行跳过、通知静默、EOF 退出。"""

    def test_loop_processes_lines_until_eof(self, dispatcher):
        stdin = io.StringIO(
            _rpc("ping") + "\n"
            "\n"  # 空白行必须跳过
            + '{"jsonrpc": "2.0", "method": "ping"}\n'  # 通知：静默
            + _rpc("ghost") + "\n"
        )
        stdout = io.StringIO()
        serve_stdio(dispatcher, stdin=stdin, stdout=stdout)
        lines = [ln for ln in stdout.getvalue().split("\n") if ln]
        assert len(lines) == 2  # 通知无响应
        assert json.loads(lines[0])["result"] == "pong"
        assert json.loads(lines[1])["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# 6. fetch_models：环回 OpenAI /models 桩（成功与三类异常映射）
# ---------------------------------------------------------------------------


class StubModelServer:
    """127.0.0.1 环回桩：按路径分流 成功 / 401 / 非 JSON，并记录鉴权头。"""

    def __init__(self) -> None:
        self.last_auth: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.last_auth = self.headers.get("Authorization")
                if self.path.startswith("/unauth"):
                    self._reply(401, '{"error": {"message": "invalid key"}}')
                elif self.path.startswith("/broken"):
                    self._reply(200, "<html>not-json</html>")
                else:
                    self._reply(200, json.dumps({"data": [{"id": "glm-4-flash"}, {"id": "glm-4-plus"}]}))

            def _reply(self, code: int, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def model_server():
    server = StubModelServer()
    yield server
    server.stop()


class TestFetchModels:
    """fetch_models：成功解析、鉴权失败、非 JSON、网络拒绝四路覆盖。"""

    def test_success_parses_model_ids(self, dispatcher, model_server):
        resp = _result(dispatcher, _rpc("fetch_models", {
            "api_base": f"http://127.0.0.1:{model_server.port}/v1",
            "api_key": "sk-test",
        }))
        assert resp["result"] == {"models": ["glm-4-flash", "glm-4-plus"]}
        assert model_server.last_auth == "Bearer sk-test"

    def test_success_without_api_key_omits_auth_header(self, dispatcher, model_server):
        resp = _result(dispatcher, _rpc(
            "fetch_models", {"api_base": f"http://127.0.0.1:{model_server.port}/v1"}))
        assert "glm-4-plus" in resp["result"]["models"]
        assert model_server.last_auth is None

    def test_http_401_maps_to_business_error(self, dispatcher, model_server):
        resp = _result(dispatcher, _rpc("fetch_models", {
            "api_base": f"http://127.0.0.1:{model_server.port}/unauth",
            "api_key": "bad-key",
        }))
        assert resp["error"]["code"] == -32000
        assert "401" in resp["error"]["message"]

    def test_non_json_response_maps_to_business_error(self, dispatcher, model_server):
        resp = _result(dispatcher, _rpc("fetch_models", {
            "api_base": f"http://127.0.0.1:{model_server.port}/broken",
        }))
        assert resp["error"]["code"] == -32000
        assert "JSON" in resp["error"]["message"]

    def test_connection_refused_maps_to_business_error(self, dispatcher, monkeypatch):
        # 宿主 Shell 可能注入 HTTP_PROXY（本机服务代理）——代理会把发往
        # 必然关闭端口的请求截胡成 HTTP 502，「连接拒绝」前提失效。
        # 剥离代理环境并丢弃 urllib 缓存的带代理 opener，让请求真实走
        # URLError（网络错误）分支，断言其归一化语义。
        proxy_vars = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
                      "HTTPS_PROXY", "https_proxy")
        for name in proxy_vars:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
        saved_opener = urllib.request._opener
        urllib.request.install_opener(None)
        try:
            resp = _result(dispatcher, _rpc("fetch_models", {"api_base": "http://127.0.0.1:1/v1"}))
        finally:
            urllib.request.install_opener(saved_opener)
        assert resp["error"]["code"] == -32000
        assert "网络" in resp["error"]["message"]
