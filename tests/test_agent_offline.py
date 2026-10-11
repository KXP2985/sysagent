"""离线测试：不依赖真实 LLM，用脚本化 Provider 验证 Agent 循环与审批链路。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentd.agent import Agent
from agentd.config import load_config
from agentd.llm import BaseProvider, LLMReply, ToolCall
from agentd.logmon import LogMonitor
from agentd.system import SystemCollector
from agentd.tools import ApprovalManager, ToolExecutor


class ScriptedProvider(BaseProvider):
    """按剧本依次返回响应。"""

    def __init__(self, script: list[LLMReply]):
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        return self.script.pop(0)


def make_reply_tool(name: str, args: dict, tc_id: str) -> LLMReply:
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": tc_id, "type": "function",
                           "function": {"name": name, "arguments": __import__("json").dumps(args)}}]}
    return LLMReply(content=None,
                    tool_calls=[ToolCall(id=tc_id, name=name, arguments=args)],
                    raw_message=msg)


async def main() -> None:
    events: list[dict] = []
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "demo.log"
    log.write_text("app started\nERROR db connection refused\n", encoding="utf-8")

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    # 使用临时环境覆盖白名单与目录
    cfg.data["whitelist"] = {
        "commands": [
            {"name": "列目录", "command": "echo hello-from-whitelist", "needs_approval": False},
            {"name": "危险命令", "command": "echo dangerous", "needs_approval": True},
        ],
        "read_dirs": [str(tmp)],
    }
    cfg.data["monitor"]["projects"] = [{
        "name": "demo", "description": "test",
        "log_files": [str(log)],
        "error_patterns": ["\\bERROR\\b"],
        "warn_patterns": [],
    }]

    notify = events.append
    system = SystemCollector()
    system.poll()
    logmon = LogMonitor(cfg.data["monitor"], notify)
    approvals = ApprovalManager(notify)
    executor = ToolExecutor(cfg, system, logmon, approvals, notify)

    # 剧本: 查状态 → 跑免审批白名单 → 跑需审批命令(挂起) → 最终回答
    provider = ScriptedProvider([
        make_reply_tool("get_system_status", {}, "t1"),
        make_reply_tool("run_whitelisted_command", {"name": "列目录"}, "t2"),
        make_reply_tool("run_whitelisted_command", {"name": "危险命令"}, "t3"),
        LLMReply(content="全部完成", raw_message={"role": "assistant", "content": "全部完成"}),
    ])
    agent = Agent(cfg, executor, provider, notify, "testhost", "TestOS")

    await agent.handle_user("检查状态并跑两个命令")
    await asyncio.sleep(0.1)

    # 应处于挂起状态
    assert agent._busy, "应处于处理中"
    pending = approvals.list_pending()
    assert len(pending) == 1 and pending[0]["description"].startswith("执行白名单命令"), pending
    print("[1] 挂起等待审批 OK:", pending[0]["description"])

    # 拒绝 → 恢复并给出最终回答
    aid = pending[0]["id"]
    assert approvals.reject(aid)
    await agent.resolve_approval(aid, approved=False)
    await asyncio.sleep(0.1)
    assert not agent._busy, "恢复后应空闲"
    assert provider.calls == 4, f"Provider 应被调用 4 次, 实际 {provider.calls}"
    chats = [e for e in events if e["type"] == "chat" and e.get("role") == "assistant"]
    assert chats[-1]["content"] == "全部完成", chats
    print("[2] 拒绝审批并恢复循环 OK")

    # 未知命令应被拒绝
    r = await executor.execute("run_whitelisted_command", {"name": "rm -rf /"})
    assert r["status"] == "error" and "白名单" in r["output"], r
    print("[3] 未知命令拒绝 OK")

    # 白名单目录外的文件读取应被拒绝
    r = await executor.execute("read_file", {"path": r"C:\Windows\win.ini"})
    assert r["status"] == "error", r
    print("[4] 目录外读取拒绝 OK")

    # 白名单目录内读取成功
    r = await executor.execute("read_file", {"path": str(log), "lines": 5})
    assert r["status"] == "ok" and "ERROR" in r["output"], r
    print("[5] 目录内读取 OK")

    # 批准路径
    provider.script.append(make_reply_tool("run_whitelisted_command", {"name": "危险命令"}, "t4"))
    provider.script.append(LLMReply(content="命令已执行",
                                    raw_message={"role": "assistant", "content": "命令已执行"}))
    await agent.handle_user("执行危险命令")
    pending = approvals.list_pending()
    assert len(pending) == 1
    assert approvals.approve(pending[0]["id"])
    await agent.resolve_approval(pending[0]["id"], approved=True)
    await asyncio.sleep(0.1)
    chats = [e for e in events if e["type"] == "chat" and e.get("role") == "assistant"]
    assert chats[-1]["content"] == "命令已执行", chats[-1]
    tools_done = [e for e in events if e.get("type") == "tool" and e.get("action") == "done"
                  and e.get("name") == "run_whitelisted_command" and "dangerous" in str(e.get("output"))]
    assert tools_done, "批准后应真实执行命令"
    print("[6] 批准执行并恢复 OK")
    print("\nALL OFFLINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
