"""QQ 机器人通道：对接 OneBot v11 HTTP（NapCat / Lagrange / go-cqhttp 等实现）。

安全边界：
- 只响应 allowed_user_ids 私聊 / allowed_group_ids 群消息
- 可选 webhook_token 校验上报来源
- 对话沿用三级审批模型：变更操作仍需「批准/拒绝」明确指令
- 协议实现与账号风险由使用者自行选择（建议官方 QQ 机器人或自建 OneBot 服务）
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

import httpx

Notify = Callable[[dict[str, Any]], None]

_APPROVE_RE = re.compile(r"^\s*(批准|同意|approve|yes|y)\s*$", re.I)
_REJECT_RE = re.compile(r"^\s*(拒绝|否决|reject|no|n)\s*$", re.I)


class QQChannel:
    def __init__(self, qq_conf: dict[str, Any], notify: Notify,
                 handle_user: Callable, decide_approval: Callable,
                 get_pending: Callable, send_to_targets: Callable | None = None):
        self.conf = qq_conf or {}
        self.enabled = bool(self.conf.get("enabled"))
        self.api_base = str(self.conf.get("api_base", "http://127.0.0.1:3000")).rstrip("/")
        self.access_token = str(self.conf.get("access_token") or "")
        self.webhook_token = str(self.conf.get("webhook_token") or "")
        self.allowed_users = {int(x) for x in self.conf.get("allowed_user_ids") or []}
        self.allowed_groups = {int(x) for x in self.conf.get("allowed_group_ids") or []}
        self.report_group_id = self.conf.get("report_group_id")
        self._notify = notify
        self._handle_user = handle_user  # async (text, source) -> None
        self._decide_approval = decide_approval  # async (id, approved) -> None
        self._get_pending = get_pending  # () -> list[dict]
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.last_targets: list[dict[str, Any]] = []  # 最近会话目标（汇报/审批回话用）

    # ---------- 入站 ----------
    def check_webhook_token(self, token: str | None) -> bool:
        return (not self.webhook_token) or (token or "") == self.webhook_token

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "reason": "qq.enabled=false"}
        if event.get("post_type") != "message":
            return {"ok": True, "ignored": "non-message"}

        msg_type = event.get("message_type")
        user_id = _as_int(event.get("user_id"))
        group_id = _as_int(event.get("group_id"))
        if msg_type == "private":
            if user_id not in self.allowed_users:
                return {"ok": False, "reason": "user not allowed"}
            target = {"type": "private", "user_id": user_id}
        elif msg_type == "group":
            if group_id not in self.allowed_groups:
                return {"ok": False, "reason": "group not allowed"}
            target = {"type": "group", "group_id": group_id}
        else:
            return {"ok": True, "ignored": "unsupported type"}

        text = _extract_text(event.get("message"), event.get("raw_message"))
        text = (text or "").strip()
        if not text:
            return {"ok": True, "ignored": "empty"}

        self._remember_target(target)

        # 审批快捷指令
        if _APPROVE_RE.match(text) or _REJECT_RE.match(text):
            approved = bool(_APPROVE_RE.match(text))
            pending = self._get_pending()
            if not pending:
                await self._send(target, "当前没有待审批的操作。")
                return {"ok": True, "action": "no-pending"}
            item = pending[0]
            await self._decide_approval(item["id"], approved)
            label = "已批准执行" if approved else "已拒绝"
            await self._send(target, f"{label}：{item.get('description') or item.get('tool')}")
            return {"ok": True, "action": "decided"}

        await self._handle_user(text, source=f"qq:{target['type']}:{user_id or group_id}")
        return {"ok": True, "action": "chat"}

    # ---------- 出站 ----------
    def remember_target(self, target: dict[str, Any]) -> None:
        self._remember_target(target)

    def _remember_target(self, target: dict[str, Any]) -> None:
        self.last_targets = ([target] + [t for t in self.last_targets if t != target])[:3]

    async def _send(self, target: dict[str, Any], text: str) -> None:
        if not text:
            return
        payload: dict[str, Any] = {"message": text}
        if target["type"] == "private":
            payload["user_id"] = target["user_id"]
            api = "/send_private_msg"
        else:
            payload["group_id"] = target["group_id"]
            api = "/send_group_msg"
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            await self._client.post(self.api_base + api, json=payload, headers=headers)
        except httpx.HTTPError as e:
            self._notify({"type": "log_error", "project": "qq",
                          "message": f"QQ 发送失败: {e}", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})

    async def forward_agent_event(self, event: dict[str, Any]) -> None:
        """把 Agent 事件转发到最近的 QQ 会话 / 汇报群。"""
        if not self.enabled:
            return
        etype = event.get("type")
        source = str(event.get("source") or "")

        # 定时汇报 → 报告群
        if etype == "chat" and event.get("report") and self.report_group_id:
            await self._send({"type": "group", "group_id": _as_int(self.report_group_id)},
                             str(event.get("content") or ""))
            return

        # QQ 会话相关事件 → 回到来源
        if not source.startswith("qq:"):
            return
        # source 格式 qq:private:123 / qq:group:456
        parts = source.split(":")
        if len(parts) < 3:
            return
        _, kind, ident = parts[0], parts[1], parts[2]
        target = {"type": kind, "user_id" if kind == "private" else "group_id": _as_int(ident)}

        if etype == "chat" and event.get("role") == "assistant":
            await self._send(target, str(event.get("content") or ""))
        elif etype == "chat_error":
            await self._send(target, f"⚠ {event.get('content')}")
        elif etype == "approval" and event.get("action") == "added":
            ap = event.get("approval") or {}
            await self._send(target,
                             f"⚠ 需要你批准：{ap.get('description')}\n回复「批准」或「拒绝」（也可到 Web 控制台操作）")
        elif etype == "capability" and event.get("action") == "added":
            cap = event.get("capability") or {}
            await self._send(target, f"🧬 能力提案：{cap.get('description')}\n请到 Web 控制台「记忆」页审批。")

    async def aclose(self) -> None:
        await self._client.aclose()


def _as_int(v: Any) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_text(message: Any, raw: Any) -> str:
    """OneBot 消息可以是字符串或 CQ 段数组。"""
    if isinstance(message, str):
        return re.sub(r"\[CQ:[^\]]*\]", " ", message)
    if isinstance(message, list):
        bits = []
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                bits.append(str(seg.get("data", {}).get("text", "")))
        return " ".join(bits)
    return str(raw or "")
