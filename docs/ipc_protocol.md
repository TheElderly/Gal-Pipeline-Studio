\# Sidecar IPC 通信协议规范 (JSON-RPC 2.0 via StdIO)



1\. 传输通道：单行 JSON 字符串传输，末尾带 `\\n`，严禁多行换行混杂。

2\. 进程调用参数：

&#x20;  `core-engine.exe --parent-pid <PID>`，环境变量强制注入 `PYTHONUNBUFFERED=1`。

3\. 核心交互方法示例：

&#x20;  - 任务派发 (WinUI -> Sidecar):

&#x20;    `{"jsonrpc": "2.0", "id": 1, "method": "pipeline.start", "params": {"game\_path": "...", "config": {...}}}`

&#x20;  - 进度推送 (Sidecar -> WinUI):

&#x20;    `{"jsonrpc": "2.0", "method": "pipeline.progress", "params": {"stage": "LQA", "current": 142, "total": 450}}`

&#x20;  - 异常告警 (Sidecar -> WinUI):

&#x20;    `{"jsonrpc": "2.0", "method": "pipeline.lqa\_error", "params": {"uuid": "chap01\_00012", "error\_code": "TAG\_MISMATCH"}}`

