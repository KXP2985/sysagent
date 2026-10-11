"""定时汇报 / QQ 通道 / 代码学习 三件套功能测试。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentd import codescan  # noqa: E402
from agentd.config import load_config  # noqa: E402
from agentd.logmon import LogMonitor  # noqa: E402
from agentd.memory import MemoryStore  # noqa: E402
from agentd.qqchannel import QQChannel  # noqa: E402
from agentd.reporting import ReportScheduler  # noqa: E402
from agentd.system import SystemCollector  # noqa: E402
from agentd.tools import ApprovalManager, ToolExecutor  # noqa: E402


def test_codescan() -> None:
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "myapp"
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname='myapp'\n", encoding="utf-8")
    (proj / "README.md").write_text("# My App\n\n风格：函数式 + 类型注解\n", encoding="utf-8")
    (proj / "src" / "main.py").write_text("def main():\n    return 42\n", encoding="utf-8")

    projects = codescan.list_projects([str(tmp)])
    assert any(p["name"] == "myapp" for p in projects), projects
    info = codescan.analyze_project(str(proj), [str(tmp)])
    assert info["project"] == "myapp" and "main.py" in info["tree"]
    assert "pyproject.toml" in info["style_files"]
    # 越界拒绝
    assert codescan.analyze_project("/etc", [str(tmp)]).get("error")
    # 脚手架：正常写入 / 路径穿越拒绝 / 重名拒绝
    r = codescan.scaffold_files(str(tmp / "ws"), "demo-app", [
        {"path": "main.py", "content": "print('hi')\n"},
        {"path": "pkg/util.py", "content": "U = 1\n"},
    ])
    assert r.get("ok") and set(r["written"]) == {"main.py", "pkg/util.py"}, r
    r = codescan.scaffold_files(str(tmp / "ws"), "evil", [{"path": "../x.py", "content": "x"}])
    assert r.get("error"), r
    r = codescan.scaffold_files(str(tmp / "ws"), "demo-app", [{"path": "a.py", "content": "1"}])
    assert r.get("error"), r
    print("[1] 代码扫描/分析/沙箱脚手架 OK")


def test_qq() -> None:
    received: list[dict] = []

    class OneBot(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

    server = ThreadingHTTPServer(("127.0.0.1", 8801), OneBot)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    handled = []
    pending = [{"id": "ap1", "description": "执行危险命令", "tool": "run_whitelisted_command"}]

    async def run():
        def handle_user(text, source=None):
            handled.append((text, source))
            return asyncio.sleep(0)

        async def decide(aid, approved):
            pending.clear()
            return None

        qq = QQChannel({
            "enabled": True, "api_base": "http://127.0.0.1:8801",
            "allowed_user_ids": [10001], "allowed_group_ids": [20001],
            "report_group_id": 20001, "webhook_token": "s3cret",
        }, notify=lambda e: None,
            handle_user=lambda text, source=None: handle_user(text, source),
            decide_approval=lambda aid, ok: decide(aid, ok),
            get_pending=lambda: list(pending))

        assert qq.check_webhook_token("s3cret") and not qq.check_webhook_token("bad")
        # 白名单外用户被拒
        r = await qq.handle_event({"post_type": "message", "message_type": "private",
                                   "user_id": 999, "message": "hello"})
        assert r.get("reason") == "user not allowed", r
        # 白名单用户 → 进入 Agent
        r = await qq.handle_event({"post_type": "message", "message_type": "private",
                                   "user_id": 10001, "message": [{"type": "text", "data": {"text": "系统状态"}}]})
        assert r.get("action") == "chat" and handled[-1][0] == "系统状态", r
        # 批准指令
        r = await qq.handle_event({"post_type": "message", "message_type": "group",
                                   "group_id": 20001, "message": "批准"})
        assert r.get("action") == "decided", r
        assert not pending
        # Agent 事件回传到 QQ
        await qq.forward_agent_event({"type": "chat", "role": "assistant",
                                      "content": "CPU 10%", "source": "qq:private:10001"})
        await qq.forward_agent_event({"type": "chat", "role": "assistant",
                                      "content": "📊 定时汇报", "source": "report", "report": True})
        await qq.aclose()
        assert any(m.get("user_id") == 10001 and "CPU" in str(m.get("message")) for m in received), received
        assert any(m.get("group_id") == 20001 and "定时汇报" in str(m.get("message")) for m in received), received

    asyncio.run(run())
    server.shutdown()
    print("[2] QQ 白名单/批准指令/回传/汇报转发 OK")


def test_report_and_tools() -> None:
    tmp = Path(tempfile.mkdtemp())
    events: list = []
    alerts = deque(maxlen=50)
    sysc = SystemCollector()
    sysc.poll()
    logmon = LogMonitor({"poll_interval": 0.1, "projects": []}, events.append)
    rep = ReportScheduler({"enabled": True, "interval_minutes": 30},
                          sysc, logmon, alerts, events.append)
    assert "定时汇报" in rep.build_digest()

    cfg = load_config(ROOT / "config.example.yaml")
    cfg.data["data_dir"] = str(tmp)
    cfg.data["code"] = {"scan_dirs": [str(tmp)], "workspace_dir": str(tmp / "ws")}
    mem = MemoryStore(tmp / "memory.jsonl")
    approvals = ApprovalManager(events.append)
    ex = ToolExecutor(cfg, sysc, logmon, approvals, events.append, memory=mem)

    # 沙箱脚手架走审批
    import asyncio

    async def run():
        r = await ex.execute("scaffold_project", {
            "name": "gen-app",
            "files": [{"path": "main.py", "content": "print(1)\n"}],
        })
        assert r["status"] == "needs_approval", r
        aid = r["approval_id"]
        assert approvals.approve(aid)
        r2 = await ex.execute("scaffold_project", {
            "name": "gen-app",
            "files": [{"path": "main.py", "content": "print(1)\n"}],
        }, approval_id=aid)
        assert r2["status"] == "ok" and "main.py" in r2["output"], r2
        assert (tmp / "ws" / "gen-app" / "main.py").exists()

        # 未配置 scan_dirs 时分析给出明确提示
        cfg.data["code"]["scan_dirs"] = []
        r3 = await ex.execute("list_code_projects", {})
        assert r3["status"] == "error" and "scan_dirs" in r3["output"]
        print("[3] 定时汇报摘要 + 脚手架审批写入 OK")

    asyncio.run(run())


if __name__ == "__main__":
    test_codescan()
    test_qq()
    test_report_and_tools()
    print("\nALL FEATURE TESTS PASSED")
