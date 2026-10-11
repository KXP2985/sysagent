"""能力进化：智能体可以为"自己加武器"，但必须经人工审批。

可提案能力（kinds）：
- whitelist_command  新增白名单命令（扩大可执行面）→ 需审批
- monitor_project    新增日志监听项目（扩大感知面）→ 需审批
- playbook           新增运维剧本（知识，直接写入记忆）→ 无需审批

审批通过后写入 data/config.runtime.yaml 并热加载（不改用户 config.yaml）。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import Config
from .memory import MemoryStore

Notify = Callable[[dict[str, Any]], None]
ApplyProject = Callable[[dict[str, Any]], None]  # logmon.add_project

PROPOSAL_KINDS = ("whitelist_command", "monitor_project")
ALL_KINDS = PROPOSAL_KINDS + ("playbook",)
KIND_LABELS = {
    "whitelist_command": "白名单命令",
    "monitor_project": "日志监听项目",
    "playbook": "运维剧本",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class CapabilityManager:
    def __init__(self, config: Config, memory: MemoryStore,
                 notify: Notify, apply_project: ApplyProject | None = None):
        self.config = config
        self.memory = memory
        self._notify = notify
        self._apply_project = apply_project
        self.runtime_path = Path(config.data.get("data_dir", "./data")).expanduser() / "config.runtime.yaml"
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending: dict[str, dict[str, Any]] = {}
        self.applied: list[dict[str, Any]] = []
        self._load_runtime()

    # ---------- runtime overlay ----------
    def _load_runtime(self) -> None:
        if not self.runtime_path.exists():
            return
        data = yaml.safe_load(self.runtime_path.read_text(encoding="utf-8")) or {}
        for item in data.get("whitelist_commands", []):
            self._apply_whitelist(item, persist=False)
            self.applied.append({**item, "_kind": "whitelist_command"})
        for item in data.get("monitor_projects", []):
            self._apply_project_item(item, persist=False)
            self.applied.append({**item, "_kind": "monitor_project"})

    def _persist(self) -> None:
        data = {
            "whitelist_commands": [
                {k: v for k, v in a.items() if not k.startswith("_")}
                for a in self.applied if a.get("_kind") == "whitelist_command"
            ],
            "monitor_projects": [
                {k: v for k, v in a.items() if not k.startswith("_")}
                for a in self.applied if a.get("_kind") == "monitor_project"
            ],
        }
        self.runtime_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _apply_whitelist(self, item: dict, persist: bool = True) -> None:
        cmds = self.config.data["whitelist"].setdefault("commands", [])
        cmds = [c for c in cmds if c.get("name") != item["name"]] + [{
            "name": item["name"],
            "command": item["command"],
            "needs_approval": bool(item.get("needs_approval", True)),
        }]
        self.config.data["whitelist"]["commands"] = cmds
        if persist:
            self._persist()

    def _apply_project_item(self, item: dict, persist: bool = True) -> None:
        projects = self.config.data["monitor"].setdefault("projects", [])
        projects = [p for p in projects if p.get("name") != item["name"]] + [dict(item)]
        self.config.data["monitor"]["projects"] = projects
        if self._apply_project:
            self._apply_project(dict(item))
        if persist:
            self._persist()

    # ---------- 提案 ----------
    def propose(self, kind: str, payload: dict[str, Any], reason: str = "") -> dict[str, Any]:
        """返回 {"status": "pending", ...} 或 {"status": "saved", ...}（剧本免审批）。"""
        if kind not in ALL_KINDS:
            return {"status": "error", "output": f"未知能力类型: {kind}，可选 {ALL_KINDS}"}
        err = self._validate(kind, payload)
        if err:
            return {"status": "error", "output": err}

        if kind == "playbook":
            entry = self.memory.add(
                "playbook", payload.get("title", "未命名剧本"),
                payload.get("content", ""),
                tags=payload.get("tags") or ["进化生成"], source="evolution",
            )
            return {"status": "ok", "output": f"运维剧本已存入记忆（id={entry['id']}）：{entry['title']}"}

        cid = uuid.uuid4().hex[:12]
        item = {
            "id": cid, "kind": kind, "payload": payload, "reason": reason,
            "description": self._describe(kind, payload),
            "status": "pending", "created": _now(),
        }
        self.pending[cid] = item
        self._notify({"type": "capability", "action": "added", "capability": item})
        return {"status": "needs_approval", "approval_id": cid, "description": item["description"]}

    def decide(self, cid: str, approved: bool) -> bool:
        item = self.pending.get(cid)
        if not item or item["status"] != "pending":
            return False
        if not approved:
            item["status"] = "rejected"
            self._notify({"type": "capability", "action": "rejected", "capability": item})
            self.pending.pop(cid, None)
            return True

        payload = item["payload"]
        self.applied.append({**payload, "_kind": item["kind"]})
        if item["kind"] == "whitelist_command":
            self._apply_whitelist(payload)
        else:
            self._apply_project_item(payload)
        item["status"] = "applied"
        self._notify({"type": "capability", "action": "applied", "capability": item})
        self.pending.pop(cid, None)
        # 同时记入记忆，方便以后回顾"能力是怎么长出来的"
        self.memory.add(
            "fact", f"新增能力：{self._describe(item['kind'], payload)}",
            f"通过智能体提案并经人工审批加入。理由：{item.get('reason') or '业务需要'}",
            tags=["能力进化", KIND_LABELS.get(item["kind"], item["kind"])], source="evolution",
        )
        return True

    # ---------- 校验与描述 ----------
    def _validate(self, kind: str, payload: dict[str, Any]) -> str | None:
        if not isinstance(payload, dict):
            return "payload 必须是对象"
        if kind == "whitelist_command":
            name = str(payload.get("name") or "").strip()
            command = str(payload.get("command") or "").strip()
            if not name or not command:
                return "whitelist_command 需要 name 与 command"
            if any(c.get("name") == name for c in self.config.whitelist_commands):
                return f"白名单命令名已存在: {name}"
        elif kind == "monitor_project":
            name = str(payload.get("name") or "").strip()
            logs = payload.get("log_files") or []
            if not name:
                return "monitor_project 需要 name"
            if not isinstance(logs, list) or not logs:
                return "monitor_project 需要非空 log_files 列表"
            # 项目名唯一
            if any(p.get("name") == name for p in self.config.projects):
                return f"项目名已存在: {name}"
        elif kind == "playbook":
            if not str(payload.get("title") or "").strip() or not str(payload.get("content") or "").strip():
                return "playbook 需要 title 与 content"
        return None

    def _describe(self, kind: str, payload: dict) -> str:
        if kind == "whitelist_command":
            ap = "需审批" if payload.get("needs_approval", True) else "直接执行"
            return f"新增白名单命令「{payload.get('name')}」: {payload.get('command')}（{ap}）"
        if kind == "monitor_project":
            files = ", ".join(str(x) for x in payload.get("log_files", []))
            return f"新增日志监听「{payload.get('name')}」: {files}"
        return f"新增运维剧本「{payload.get('title')}」"

    # ---------- 查询 ----------
    def list_pending(self) -> list[dict[str, Any]]:
        return list(self.pending.values())

    def list_applied(self) -> list[dict[str, Any]]:
        return [
            {"kind": a.get("_kind"), "name": a.get("name") or a.get("title"),
             "command": a.get("command"), "log_files": a.get("log_files"),
             "needs_approval": a.get("needs_approval")}
            for a in self.applied
        ]
