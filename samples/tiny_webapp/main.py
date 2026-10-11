"""tiny_webapp 入口：演示类型注解与 logging 风格。"""
from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tiny_webapp")


def handle(path: str) -> str:
    if path == "/health":
        return "ok"
    return f"hello {path}"


def main() -> None:
    log.info("tiny_webapp listening on :8080")
    for path in ("/health", "/demo"):
        log.info("GET %s -> %s", path, handle(path))


if __name__ == "__main__":
    main()
