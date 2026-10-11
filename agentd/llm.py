"""LLM Provider 抽象：OpenAI 兼容服务与 Ollama 本地模型，统一为工具调用响应。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)  # 存入历史的 assistant 消息


def _to_args(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"_raw": value}
    return {"value": value}


class BaseProvider:
    name = "base"

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> LLMReply:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class OpenAICompatible(BaseProvider):
    """兼容 /v1/chat/completions：OpenAI、DeepSeek、通义、MiMo 等。"""

    name = "openai_compatible"

    def __init__(self, conf: dict[str, Any]):
        self.base_url = str(conf.get("base_url", "")).rstrip("/")
        self.api_key = conf.get("api_key") or ""
        self.model = conf.get("model", "gpt-4o-mini")
        self.temperature = float(conf.get("temperature", 0.3))
        self.timeout = float(conf.get("timeout", 60))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions", json=body, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 请求失败: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM 响应非 JSON: {e}") from e

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"LLM 响应结构异常: {json.dumps(data, ensure_ascii=False)[:500]}") from e

        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            tool_calls.append(ToolCall(
                id=tc.get("id") or fn.get("name", "call"),
                name=fn.get("name", ""),
                arguments=_to_args(fn.get("arguments")),
            ))
        stored = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            stored["tool_calls"] = msg["tool_calls"]
        return LLMReply(content=msg.get("content"), tool_calls=tool_calls, raw_message=stored)

    async def aclose(self) -> None:
        await self._client.aclose()


class Ollama(BaseProvider):
    """本地 Ollama /api/chat（建议 ≥ 0.3.7 以支持 tools）。"""

    name = "ollama"

    def __init__(self, conf: dict[str, Any]):
        self.base_url = str(conf.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = conf.get("model", "qwen2.5:7b")
        self.timeout = float(conf.get("timeout", 120))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    @staticmethod
    def _to_ollama_messages(messages: list[dict]) -> list[dict]:
        """OpenAI 风格历史 → Ollama /api/chat 格式：
        arguments 必须是对象，工具结果消息用 tool_name 而不是 tool_call_id。"""
        out: list[dict[str, Any]] = []
        id_to_name: dict[str, str] = {}
        for m in messages:
            role = m.get("role")
            if role == "assistant":
                tcs = m.get("tool_calls")
                if not tcs:
                    out.append({"role": "assistant", "content": m.get("content") or ""})
                    continue
                new_tcs = []
                for tc in tcs:
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args else {}
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    name = fn.get("name", "")
                    id_to_name[str(tc.get("id") or name)] = name
                    new_tcs.append({"function": {"name": name, "arguments": args or {}}})
                out.append({"role": "assistant", "content": m.get("content") or "",
                            "tool_calls": new_tcs})
            elif role == "tool":
                name = (m.get("name") or id_to_name.get(str(m.get("tool_call_id")))
                        or id_to_name.get(str(m.get("tool_call_id") or "").split(".")[-1]) or "")
                out.append({"role": "tool", "tool_name": name,
                            "content": str(m.get("content") or "")})
            else:
                out.append(m)
        return out

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_ollama_messages(messages),
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        try:
            resp = await self._client.post(f"{self.base_url}/api/chat", json=body)
            if resp.status_code >= 400:
                raise LLMError(f"Ollama 返回 {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama 请求失败（确认服务已启动: {self.base_url}）: {e}") from e

        msg = data.get("message", {})
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            tool_calls.append(ToolCall(
                id=fn.get("name", "call"),
                name=fn.get("name", ""),
                arguments=_to_args(fn.get("arguments")),
            ))
        stored = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            stored["tool_calls"] = [
                {"id": tc.get("function", {}).get("name", "call"),
                 "type": "function",
                 "function": {
                     "name": tc.get("function", {}).get("name", ""),
                     "arguments": json.dumps(
                         _to_args(tc.get("function", {}).get("arguments")),
                         ensure_ascii=False),
                 }}
                for tc in msg["tool_calls"]
            ]
        return LLMReply(content=msg.get("content"), tool_calls=tool_calls, raw_message=stored)

    async def aclose(self) -> None:
        await self._client.aclose()


def create_provider(cfg) -> BaseProvider:
    provider = cfg.llm_provider
    conf = cfg.llm_conf
    if provider == "ollama":
        return Ollama(conf)
    return OpenAICompatible(conf)
