"""127.0.0.1 环回 Mock LLM 服务 —— 全离线 Dogfooding 专用（零外网请求）。

严格绑定本地回环地址 127.0.0.1:18080，解析 OpenAI 兼容
POST /v1/chat/completions；提取批次提示词，按编号返回罐头译文：
第 2 句刻意缺失闭直角引号（LQA 拦截靶子），其余为规范成对引号译文。
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

REPLIES = {
    1: "「知道真相的觉悟，我已经准备好了。」",
    2: "「想起的是，那个雨夜的事情。",  # 刻意缺失闭引号」→ LQA_FAILED 靶子
    3: "被折断伞的我，浑身湿透地站在家门口。",
    4: "「那时候，千代什么也没说。」",
    5: "「只是选择了贯彻真实而已。」",
    6: "只有浪声，在两人之间流淌。",
    7: "「拉钩上吊，说谎就吞一千根针。」",
    8: "「……这个约定，我至今仍遵守着。」",
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        user = payload["messages"][-1]["content"]
        print("[mock] 收到批次提示词:", user.replace("\n", " | "), flush=True)
        content = "\n".join(f"{n}. {REPLIES[n]}" for n in sorted(REPLIES))
        body = json.dumps(
            {
                "id": "chatcmpl-loopback-mock",
                "object": "chat.completion",
                "model": payload.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 18080), Handler)
    print("[mock] listening on 127.0.0.1:18080", flush=True)
    server.serve_forever()
