"""路径解析：源码项目模式与 PyInstaller 便携（冻结）模式统一入口。

- 源码模式:   根目录 = agentd/ 的上一级（项目根），按 CWD 或 AGENTD_HOME 找 config
- 便携模式:   根目录 = exe 所在目录；config/data/logs 都在 exe 旁，可整个文件夹拷走
- 静态资源:   源码 = <root>/static；冻结 = PyInstaller 的 _MEIPASS/static
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """可写根目录（config.yaml / data / logs 所在处）。

    便携模式: exe 同目录（数据跟着文件夹走）
    安装模式: exe 目录存在 installed.flag（正式安装写入）→ 数据放 %APPDATA%\\SysAgent
    源码模式: 项目根目录
    """
    env = os.environ.get("AGENTD_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "installed.flag").exists():
            appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(appdata) / "SysAgent"
        return exe_dir
    # 源码模式: 优先当前工作目录（python -m agentd 在项目根启动），否则包的上一级
    cwd = Path.cwd().resolve()
    if (cwd / "agentd").is_dir() or (cwd / "config.yaml").exists() or (cwd / "config.example.yaml").exists():
        return cwd
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """只读资源根目录（static/）。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    return resource_root() / "static"


def config_path() -> Path:
    env = os.environ.get("AGENTD_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return app_root() / "config.yaml"


def bootstrap() -> Path:
    """首启引导：确保 config.yaml / data / logs 就位，返回应用根目录。"""
    root = app_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    cfg = root / "config.yaml"
    if not cfg.exists():
        # 冻结包内携带模板；源码模式用项目里的模板
        template = resource_root() / "config.example.yaml"
        if template.exists():
            text = template.read_text(encoding="utf-8")
        else:
            text = "# SysAgent 配置\nllm:\n  provider: ollama\n"
        cfg.write_text(text, encoding="utf-8")
    return root
