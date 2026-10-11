"""Agent 循环：用户指令 → LLM → 工具调用 → （审批）→ 结果回填 → 最终回答。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from .config import Config
from .llm import BaseProvider, LLMError
from .tools import ToolExecutor

Notify = Callable[[dict[str, Any]], None]

_SYSTEM_PROMPT = """你是 SysAgent，一个部署在本机（{hostname}，{os}）的运维智能体。
你的职责：
1. 感知与分析系统状态（CPU/内存/磁盘/网络/进程），按用户指令给出分析结论；
2. 监听已配置项目的后台日志，出问题时定位原因并给出解决建议；
3. 通过白名单工具执行运维操作；改变系统状态的操作会触发人工审批，被拒绝时如实告知；
4. 自我学习：把搞清楚的业务事实、排障经验、可复用的运维步骤沉淀进长期记忆，越用越懂业务；
5. 自我进化：业务出现新需求时（缺少命令、缺少日志入口），用 propose_capability 提案新能力，
   由用户审批后扩宽你的能力库——不得绕过审批、不得提案与当前任务无关的危险命令。

工作方式：
- 回答前先用工具取证（get_system_status / read_project_log 等），不要凭空猜测系统状态；
- 日志类问题：先看相关项目最近日志，找出出错位置与首次出现时间，再结合系统状态分析根因；
- 涉及业务约定、历史故障时先 recall 检索记忆，与取证结果交叉印证；
- 故障定位出根因或方案落地验证后，调用 remember 沉淀 kind=lesson（现象/根因/方案/适用范围）；
- 新弄清楚的业务事实（拓扑、端口、负责人、依赖）用 remember 存 kind=fact；
- 可复用的标准处理步骤用 propose_capability 提案 kind=playbook；
- 给出解决方案时要具体、可操作，并明确标注哪些是推测、哪些有日志/状态证据/记忆依据；
- 做不到或不确定的事情直接说做不到/不确定，绝不编造；
- 回答使用与用户相同的语言（默认中文），简洁分点。
"""


class Agent:
    def __init__(self, config: Config, executor: ToolExecutor,
                 provider: BaseProvider, notify: Notify, hostname: str, os_name: str):
        self.config = config
        self.executor = executor
        self.provider = provider
        self._notify = notify
        self.history: list[dict[str, Any]] = []
        self.system_prompt = _SYSTEM_PROMPT.format(hostname=hostname, os=os_name)
        self._busy = False
        self._source: str | None = None
        self._suspended: dict[str, list[dict[str, Any]]] = {}

    def _emit(self, event: dict[str, Any]) -> None:
        if self._source:
            event.setdefault("source", self._source)
        self._notify(event)

    def _build_messages(self) -> list[dict[str, Any]]:
        """系统提示词 + 动态记忆摘要 + 对话历史。"""
        query = ""
        for m in reversed(self.history):
            if m.get("role") == "user":
                query = str(m.get("content", ""))
                break
        digest = ""
        memory = self.executor.memory
        if memory is not None:
            try:
                digest = memory.digest(query, limit=6)
            except Exception:
                digest = ""
        system = self.system_prompt
        if digest:
            system = system + "\n\n" + digest
        return [{"role": "system", "content": system}] + self.history

    # ---------- 对外入口 ----------
    async def handle_user(self, text: str, source: str | None = None) -> None:
        if self._busy:
            self._emit({"type": "chat", "role": "assistant",
                        "content": "我正在处理上一条指令，请稍候或等待其完成。"})
            return
        self._source = source
        self.executor.context_source = source
        self.history.append({"role": "user", "content": text})
        self._emit({"type": "chat", "role": "user", "content": text})
        await self._run()

    async def resolve_approval(self, approval_id: str, approved: bool) -> None:
        entries = self._suspended.pop(approval_id, None)
        if entries is None:
            return  # 不属于当前挂起的审批
        for entry in entries:
            if approved:
                result = await self.executor.execute(
                    entry["name"], entry["args"], approval_id=approval_id)
            else:
                result = {"status": "error", "output": "用户在 Web 控制台拒绝了该操作"}
            text = str(result.get("output", ""))
            for msg in reversed(self.history):
                if msg.get("role") == "tool" and msg.get("tool_call_id") == entry["tool_call_id"]:
                    msg["content"] = text
                    break
            self._emit({"type": "tool", "action": "done",
                        "name": entry["name"], "id": entry["tool_call_id"],
                        "status": result.get("status"), "output": text[:400]})
        # 所有挂起审批均已裁决 → 恢复循环
        if not self._suspended:
            await self._run()

    # ---------- 内部 ----------
    async def _run(self) -> None:
        self._busy = True
        try:
            rounds = 0
            max_rounds = int(self.config.data["agent"].get("max_tool_rounds", 15))
            while rounds < max_rounds:
                rounds += 1
                try:
                    reply = await self.provider.chat(self._build_messages(), self.executor.schemas())
                except LLMError as e:
                    self._emit({"type": "chat_error", "content": str(e)})
                    return
                if reply.raw_message:
                    self.history.append(reply.raw_message)

                if not reply.tool_calls:
                    content = reply.content or "(空回答)"
                    self._emit({"type": "chat", "role": "assistant", "content": content})
                    self._trim()
                    return

                suspended_here: list[tuple[str, dict]] = []
                for tc in reply.tool_calls:
                    self._emit({"type": "tool", "action": "start",
                                  "name": tc.name, "args": tc.arguments,
                                  "id": tc.id})
                    result = await self.executor.execute(tc.name, tc.arguments)

                    if result.get("status") == "needs_approval":
                        aid = result["approval_id"]
                        suspended_here.append((aid, {
                            "approval_id": aid, "tool_call_id": tc.id,
                            "name": tc.name, "args": tc.arguments, "result": None,
                        }))
                        self._emit({"type": "tool", "action": "await_approval",
                                      "name": tc.name, "args": tc.arguments,
                                      "id": tc.id, "approval_id": aid})
                        content = f"等待用户审批 {aid}…"
                    else:
                        preview = result.get("output", "")
                        if len(preview) > 400:
                            preview = preview[:400] + "…"
                        self._emit({"type": "tool", "action": "done",
                                      "name": tc.name, "id": tc.id,
                                      "status": result.get("status"),
                                      "output": preview})
                        content = result.get("output", "")

                    self.history.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": str(content),
                    })

                if suspended_here:
                    # 挂起: 记录待回填的 tool 消息，等审批后继续
                    for aid, entry in suspended_here:
                        self._suspended.setdefault(aid, []).append(entry)
                    return  # resolve_approval 会在全部裁决后重新进入 _run

            self._emit({"type": "chat", "role": "assistant",
                          "content": f"已达到单次最大工具轮数（{max_rounds}），"
                                     f"先给出阶段性结论，可继续追问。"})
            self._trim()
        finally:
            if not self._suspended:
                self._busy = False

    def _trim(self) -> None:
        limit = int(self.config.data["agent"].get("history_limit", 40))
        if len(self.history) <= limit:
            return
        user_idx = [i for i, m in enumerate(self.history) if m.get("role") == "user"]
        if len(user_idx) >= 2:
            del self.history[:user_idx[1]]  # 按轮次边界裁剪，避免拆散 tool 配对
