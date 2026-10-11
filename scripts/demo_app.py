"""演示应用：向 logs/demo.log 写日志，模拟一个有故障的后台服务。

用法: python scripts/demo_app.py
由 SysAgent 的 start_project 启动时，控制台输出会被重定向到同一日志文件。
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "demo.log"


def log(line: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{ts} {line}"
    print(text, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def main() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log("INFO demo_app starting pid=%d" % __import__("os").getpid())
    n = 0
    while True:
        n += 1
        time.sleep(2)
        r = random.random()
        if r < 0.12:
            log("ERROR database connection refused (retry=%d)" % n)
        elif r < 0.22:
            log("WARN cache miss rate high, consider increasing cache size")
        elif n % 30 == 0:
            log("Traceback (most recent call last):  # simulated")
            log('  File "demo_app.py", line 1, in <module>')
            log("RuntimeError: simulated worker crash")
        else:
            log("INFO heartbeat #%d ok" % n)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("INFO demo_app stopped by user")
        sys.exit(0)
