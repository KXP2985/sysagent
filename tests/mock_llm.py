"""脚本化 OpenAI 兼容 Mock LLM：按调用次数返回预设响应，供离线 E2E 使用。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 剧本：查状态 → 记忆沉淀 → 能力提案 → 需审批命令（挂起）→ 最终回答
SCRIPT = [
    {
        "content": None,
        "tool_calls": [{
            "id": "t1", "type": "function",
            "function": {"name": "get_system_status", "arguments": "{}"},
        }],
    },
    {
        "content": None,
        "tool_calls": [{
            "id": "t2", "type": "function",
            "function": {"name": "remember", "arguments": json.dumps({
                "kind": "lesson", "title": "e2e 注入故障处置",
                "content": "现象：db down；方案：重启连接池；适用：demo 环境",
                "tags": ["e2e", "demo"]}, ensure_ascii=False)},
        }],
    },
    {
        "content": None,
        "tool_calls": [{
            "id": "t3", "type": "function",
            "function": {"name": "propose_capability", "arguments": json.dumps({
                "kind": "whitelist_command",
                "payload": {"name": "查看内存", "command": "echo MEM-MOCK", "needs_approval": False},
                "reason": "排障需要快速看内存"}, ensure_ascii=False)},
        }],
    },
    {
        "content": None,
        "tool_calls": [{
            "id": "t4", "type": "function",
            "function": {"name": "run_whitelisted_command",
                         "arguments": json.dumps({"name": "重启演示应用"}, ensure_ascii=False)},
        }],
    },
    {
        "content": "脚本完成：已查状态、沉淀经验、提案新能力，并请求了需审批命令。",
        "tool_calls": None,
    },
]


class Handler(BaseHTTPRequestHandler):
    call_no = 0

    def log_message(self, *args):  # 安静
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        idx = min(Handler.call_no, len(SCRIPT) - 1)
        Handler.call_no += 1
        msg = SCRIPT[idx]
        resp = {
            "id": f"chatcmpl-mock-{idx}",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": msg["content"],
                    "tool_calls": msg["tool_calls"],
                },
                "finish_reason": "tool_calls" if msg["tool_calls"] else "stop",
            }],
        }
        data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server(port: int = 8799) -> ThreadingHTTPServer:
    Handler.call_no = 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"mock LLM listening on http://127.0.0.1:{port}/v1")
    start_server(port)
    threading.Event().wait()
