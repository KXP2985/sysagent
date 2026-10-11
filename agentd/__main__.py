"""入口: python -m agentd [--config path] [--host] [--port]
便携版入口: SysAgent.exe（参数相同），数据与配置在 exe 同目录。
"""
from __future__ import annotations

import argparse

from .paths import bootstrap


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentd", description="SysAgent 服务")
    parser.add_argument("--config", default=None, help="配置文件路径 (默认 <应用根>/config.yaml)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = bootstrap()  # 确保 config/data/logs 就位
    from .config import load_config

    cfg = load_config(args.config)
    if args.host:
        cfg.data["server"]["host"] = args.host
    if args.port:
        cfg.data["server"]["port"] = args.port

    from .server import run

    print(f"[SysAgent] 应用根目录: {root}")
    run(cfg)


if __name__ == "__main__":
    main()
