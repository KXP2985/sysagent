"""配置加载：读取 config.yaml，展开 ${ENV}，提供默认值。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "ollama",
        "openai_compatible": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o-mini",
            "temperature": 0.3,
            "timeout": 60,
        },
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5:7b",
            "timeout": 120,
        },
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "agent": {"max_tool_rounds": 15, "history_limit": 40},
    "data_dir": "./data",
    "report": {"enabled": True, "interval_minutes": 30, "llm_summary": False},
    "qq": {
        "enabled": False,
        "api_base": "http://127.0.0.1:3000",
        "access_token": "",
        "webhook_token": "",
        "allowed_user_ids": [],
        "allowed_group_ids": [],
        "report_group_id": None,
    },
    "code": {"scan_dirs": [], "workspace_dir": "./workspace"},
    "whitelist": {"commands": [], "read_dirs": []},
    "monitor": {
        "poll_interval": 0.5,
        "max_lines_per_push": 200,
        "projects": [],
    },
}


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path

    @property
    def llm_provider(self) -> str:
        return self.data["llm"].get("provider", "openai_compatible")

    @property
    def llm_conf(self) -> dict[str, Any]:
        return self.data["llm"][self.llm_provider]

    @property
    def host(self) -> str:
        return str(self.data["server"].get("host", "127.0.0.1"))

    @property
    def port(self) -> int:
        return int(self.data["server"].get("port", 8765))

    @property
    def projects(self) -> list[dict[str, Any]]:
        return self.data["monitor"].get("projects", [])

    @property
    def whitelist_commands(self) -> list[dict[str, Any]]:
        return self.data["whitelist"].get("commands", [])

    @property
    def read_dirs(self) -> list[str]:
        return [str(Path(d).expanduser().resolve()) for d in self.data["whitelist"].get("read_dirs", [])]

    def public_snapshot(self) -> dict[str, Any]:
        """给前端的配置概览（隐去 api_key）。"""
        conf = dict(self.data["llm"])
        conf["openai_compatible"] = dict(conf.get("openai_compatible", {}))
        key = conf["openai_compatible"].get("api_key", "")
        conf["openai_compatible"]["api_key"] = ("***" + key[-4:]) if key else "(未设置)"
        return {
            "llm": conf,
            "projects": [{"name": p.get("name", "?"),
                          "description": p.get("description", ""),
                          "log_files": p.get("log_files", [])}
                         for p in self.projects],
            "whitelist": [
                {"name": c.get("name"), "command": c.get("command"),
                 "needs_approval": bool(c.get("needs_approval"))}
                for c in self.whitelist_commands
            ],
        }


def load_config(path: str | Path | None = None) -> Config:
    from .paths import app_root, config_path as _default_config_path

    if path is None:
        p = _default_config_path()
    else:
        p = Path(path).expanduser().resolve()
    user_data: dict[str, Any] = {}
    if p.exists():
        user_data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    elif path is not None:
        raise FileNotFoundError(f"配置文件不存在: {p}")
    merged = _deep_merge(DEFAULTS, user_data)
    merged = _expand_env(merged)

    # 相对路径统一相对应用根目录（便携模式下 exe 目录），与启动时 CWD 无关
    root = app_root()

    def _abs(key_path: tuple[str, ...], default: str) -> None:
        node: Any = merged
        for k in key_path[:-1]:
            node = node.setdefault(k, {})
        raw = node.get(key_path[-1], default)
        pp = Path(str(raw)).expanduser()
        node[key_path[-1]] = str(pp if pp.is_absolute() else (root / pp).resolve())

    _abs(("data_dir",), "./data")
    code = merged.setdefault("code", {})
    ws = Path(str(code.get("workspace_dir", "./workspace"))).expanduser()
    code["workspace_dir"] = str(ws if ws.is_absolute() else (root / ws).resolve())
    scan = code.get("scan_dirs") or []
    code["scan_dirs"] = [
        str(Path(d).expanduser()) if Path(d).expanduser().is_absolute()
        else str((root / Path(d)).resolve())
        for d in scan
    ]
    return Config(merged, p)
