"""全链路 E2E：Mock LLM + 真实服务进程 + WebSocket 驱动。

覆盖：hello 同步 / 实时日志 / 告警推送 / 对话工具循环 / 审批批准执行。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import websockets  # noqa: E402

from tests.mock_llm import start_server as start_mock  # noqa: E402

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"


async def wait_msg(ws, pred, timeout=20.0, label=""):
    """持续收消息直到 pred 命中，返回该消息。"""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
        msg = json.loads(raw)
        seen.append(msg.get("type"))
        if pred(msg):
            return msg
    raise AssertionError(f"超时未等到 {label or pred}；已见事件类型: {seen}")


async def main() -> None:
    mock = start_mock(8799)
    log_path = ROOT / "logs" / "demo.log"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text("INFO e2e start\n", encoding="utf-8")

    # 每次从干净的记忆/能力运行时开始
    import shutil
    shutil.rmtree(ROOT / "data" / "e2e", ignore_errors=True)

    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentd", "--config", "tests/config.e2e.yaml"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # 等服务就绪
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{BASE}/api/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("服务未启动")

        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello", hello
            assert hello["projects"] == ["demo"], hello["projects"]
            assert "openai_compatible" in str(hello["config"]["llm"]["provider"])
            print("[1] hello 同步 OK")

            # 实时日志 + 告警
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("ERROR e2e injected failure db down\n")
            line_msg = await wait_msg(ws, lambda m: m.get("type") == "line" and "e2e injected" in m.get("line", ""), label="日志行")
            alert_msg = await wait_msg(ws, lambda m: m.get("type") == "alert" and "e2e injected" in m.get("line", ""), label="告警")
            assert alert_msg["level"] == "error" and alert_msg["project"] == "demo"
            print("[2] 日志实时推送 + 告警 OK")

            # 对话：工具循环 + 记忆 + 能力提案 + 审批
            await ws.send(json.dumps({"type": "chat", "text": "检查系统状态并执行相关命令"}, ensure_ascii=False))
            await wait_msg(ws, lambda m: m.get("type") == "tool" and m.get("name") == "get_system_status" and m.get("action") == "done",
                           label="get_system_status 完成")
            await wait_msg(ws, lambda m: m.get("type") == "tool" and m.get("name") == "remember"
                           and m.get("action") == "done" and "已记住" in str(m.get("output", "")),
                           label="记忆沉淀")
            await wait_msg(ws, lambda m: m.get("type") == "capability" and m.get("action") == "added"
                           and m.get("capability", {}).get("kind") == "whitelist_command",
                           label="能力提案")
            print("[3] 工具循环 + 记忆沉淀 + 能力提案 OK")

            # 批准能力进化
            caps_before = json.loads(urllib.request.urlopen(f"{BASE}/api/capabilities").read())
            pend_ids = [p["id"] for p in caps_before["pending"]]
            assert pend_ids, caps_before
            await ws.send(json.dumps({"type": "cap_approve", "id": pend_ids[0]}))
            await wait_msg(ws, lambda m: m.get("type") == "capability" and m.get("action") == "applied",
                           label="能力生效")
            caps_after = json.loads(urllib.request.urlopen(f"{BASE}/api/capabilities").read())
            assert any(a.get("name") == "查看内存" for a in caps_after["applied"]), caps_after
            print("[4] 能力提案批准并热加载 OK")

            approval = await wait_msg(ws, lambda m: m.get("type") == "approval" and m.get("action") == "added",
                                      label="审批卡片")
            aid = approval["approval"]["id"]
            assert "重启演示应用" in approval["approval"]["description"]

            await ws.send(json.dumps({"type": "approve", "id": aid}))
            await wait_msg(ws, lambda m: m.get("type") == "tool" and m.get("action") == "done"
                           and "RESTART-MOCK-DONE" in str(m.get("output", "")),
                           label="批准后命令执行")
            final = await wait_msg(ws, lambda m: m.get("type") == "chat" and m.get("role") == "assistant",
                                   label="最终回答")
            assert "脚本完成" in final["content"]
            print("[5] 批准执行 + 最终回答 OK:", final["content"][:40])

        # REST 检查
        with urllib.request.urlopen(f"{BASE}/api/status") as r:
            st = json.loads(r.read())
            assert st["cpu"]["cores_logical"] > 0 and "history" in st
        with urllib.request.urlopen(f"{BASE}/api/alerts") as r:
            alerts = json.loads(r.read())
            assert any("e2e injected" in a.get("line", "") for a in alerts)
        with urllib.request.urlopen(f"{BASE}/api/projects/demo/lines?n=50") as r:
            rows = json.loads(r.read())
            assert any("e2e injected" in x.get("line", "") for x in rows)
        with urllib.request.urlopen(f"{BASE}/api/memory") as r:
            mem = json.loads(r.read())
            assert any(e.get("kind") == "lesson" and "e2e" in str(e.get("tags")) for e in mem), mem
        print("[6] REST 检查（含记忆）OK")
        print("\nALL E2E TESTS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
