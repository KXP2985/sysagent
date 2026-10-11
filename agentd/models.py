"""模型注册表：管理本地 Ollama 与云端 OpenAI 兼容模型，支持运行时切换默认。

持久化在 data/models.json；config.yaml 的 llm 段仅作为首次启动的种子配置。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .llm import BaseProvider, LLMError, OpenAICompatible, Ollama


def _mask(key: str | None) -> str:
    if not key:
        return ""
    return "***" + key[-4:] if len(key) >= 4 else "***"


class ModelRegistry:
    def __init__(self, config: Config):
        self.path = Path(config.data.get("data_dir", "./data")).expanduser() / "models.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {"active": "", "providers": []}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        if not self._data.get("providers"):
            self._seed(config)
        if not self._data.get("active"):
            self._data["active"] = self._data["providers"][0]["id"]
        self._save()

    # ---------- 种子 ----------
    def _seed(self, config: Config) -> None:
        llm = config.data.get("llm") or {}
        oc = llm.get("openai_compatible") or {}
        self._data["providers"].append({
            "id": "ollama",
            "kind": "ollama",
            "name": "本地 Ollama",
            "base_url": (llm.get("ollama") or {}).get("base_url", "http://127.0.0.1:11434"),
            "api_key": "",
            "model": (llm.get("ollama") or {}).get("model", "qwen2.5:7b"),
            "temperature": 0.3,
            "timeout": float((llm.get("ollama") or {}).get("timeout", 120)),
            "builtin": True,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._data["providers"].append({
            "id": "openai-compat",
            "kind": "openai_compatible",
            "name": "OpenAI 兼容（config 种子）",
            "base_url": oc.get("base_url", "https://api.openai.com/v1"),
            "api_key": oc.get("api_key", ""),
            "model": oc.get("model", "gpt-4o-mini"),
            "temperature": float(oc.get("temperature", 0.3)),
            "timeout": float(oc.get("timeout", 60)),
            "builtin": True,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._data["active"] = llm.get("provider", "ollama")
        # active 存的是 id；provider 名做一次对齐
        if self._data["active"] == "ollama":
            self._data["active"] = "ollama"
        elif self._data["active"] == "openai_compatible":
            self._data["active"] = "openai-compat"

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ---------- 查询 ----------
    def list_public(self) -> list[dict[str, Any]]:
        out = []
        for p in self._data["providers"]:
            q = dict(p)
            q["api_key"] = _mask(p.get("api_key"))
            q["has_key"] = bool(p.get("api_key"))
            q["active"] = p["id"] == self._data.get("active")
            out.append(q)
        return out

    def get(self, pid: str) -> dict[str, Any] | None:
        for p in self._data["providers"]:
            if p["id"] == pid:
                return p
        return None

    @property
    def active_id(self) -> str:
        return str(self._data.get("active") or "")

    def active_conf(self) -> tuple[str, dict[str, Any]]:
        p = self.get(self.active_id) or self._data["providers"][0]
        return p["kind"], _llm_conf_of(p)

    # ---------- 变更 ----------
    def add(self, name: str, base_url: str, api_key: str, model: str,
            temperature: float = 0.3, timeout: float = 60) -> dict[str, Any]:
        entry = {
            "id": "cloud-" + uuid.uuid4().hex[:8],
            "kind": "openai_compatible",
            "name": (name or "").strip() or model,
            "base_url": (base_url or "").strip().rstrip("/"),
            "api_key": (api_key or "").strip(),
            "model": (model or "").strip(),
            "temperature": float(temperature),
            "timeout": float(timeout),
            "builtin": False,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if not entry["base_url"] or not entry["model"]:
            raise ValueError("base_url 与 model 必填")
        self._data["providers"].append(entry)
        self._save()
        return entry

    def update(self, pid: str, **fields: Any) -> dict[str, Any]:
        p = self.get(pid)
        if p is None:
            raise KeyError(pid)
        for k in ("name", "base_url", "model", "temperature", "timeout"):
            if k in fields and fields[k] is not None:
                p[k] = fields[k]
        if fields.get("api_key"):  # 空值表示不修改
            p["api_key"] = fields["api_key"]
        self._save()
        return p

    def remove(self, pid: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        if p.get("builtin") and len(self._data["providers"]) <= 2:
            return False  # 保留至少内置项
        self._data["providers"] = [x for x in self._data["providers"] if x["id"] != pid]
        if self._data.get("active") == pid:
            self._data["active"] = self._data["providers"][0]["id"]
        self._save()
        return True

    def set_active(self, pid: str) -> dict[str, Any]:
        p = self.get(pid)
        if p is None:
            raise KeyError(pid)
        self._data["active"] = pid
        self._save()
        return p

    # ---------- 实例 ----------
    def create_provider(self, pid: str | None = None) -> BaseProvider:
        p = self.get(pid) if pid else (self.get(self.active_id) or self._data["providers"][0])
        if p is None:
            raise LLMError("没有可用的模型配置")
        conf = _llm_conf_of(p)
        if p["kind"] == "ollama":
            return Ollama(conf)
        return OpenAICompatible(conf)


def _llm_conf_of(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_url": p.get("base_url"),
        "api_key": p.get("api_key", ""),
        "model": p.get("model"),
        "temperature": p.get("temperature", 0.3),
        "timeout": p.get("timeout", 60),
    }


async def ping_provider(kind: str, conf: dict[str, Any]) -> str:
    """最小连通性测试：发一条 ping，返回模型回复或抛错。"""
    if kind == "ollama":
        client = Ollama(conf)
    else:
        client = OpenAICompatible(conf)
    try:
        reply = await client.chat([{"role": "user", "content": "ping，只回复 pong"}], None)
        return (reply.content or "(空回复)")[:80]
    finally:
        await client.aclose()
