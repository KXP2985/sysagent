"""日志实时监听：后台线程轮询 tail，规则命中即产生告警事件。

- 支持日志轮转/截断（文件变小自动重定位）
- 二进制偏移读取 + 增量解码，超大文件不回读全量
- 告警节流：同文案 10s 内去重，单文件 10s 内最多 5 条后改发汇总
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EventCallback = Callable[[dict[str, Any]], None]  # 由服务端保证线程安全投递

_READ_CHUNK = 4 * 1024 * 1024       # 单周期最大读取
_INITIAL_TAIL = 1 * 1024 * 1024     # 启动时只追尾 1MB 以内
_DEDUP_WINDOW = 10.0                # 同文案去重窗口（秒）
_FILE_ALERT_LIMIT = 5               # 单文件 10s 内告警上限
_FILE_ALERT_WINDOW = 10.0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _compile(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p))
        except re.error as e:
            print(f"[logmon] 无效正则忽略: {p!r} ({e})")
    return out


class FileTailer:
    """单个日志文件的增量跟踪。"""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.started = False
        self.line_no = 0  # 本次会话读到的行号（近似）

    def _begin(self, size: int) -> None:
        """首次见到文件：过大则只从尾部 1MB 开始，避免启动洪峰。"""
        self.offset = max(size - _INITIAL_TAIL, 0)
        self.partial = b""
        self.started = True

    def read_new(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            self.started = False
            return []
        if not self.started:
            self._begin(size)
        if size < self.offset:
            # 轮转或截断：重新从头跟踪新内容
            self.offset = 0
            self.partial = b""
        if size == self.offset:
            return []

        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(_READ_CHUNK)
        except OSError:
            return []
        self.offset += len(chunk)

        data = self.partial + chunk
        raw_lines = data.split(b"\n")
        self.partial = raw_lines.pop()
        lines: list[str] = []
        for raw in raw_lines:
            text = raw.decode("utf-8", errors="replace").rstrip("\r")
            self.line_no += 1
            lines.append(text)
        return lines


class ProjectMonitor:
    """一个项目 = 若干日志文件 + 若干告警规则。"""

    def __init__(self, spec: dict[str, Any], callback: EventCallback, poll_interval: float):
        self.spec = spec
        self.name = spec.get("name", "unnamed")
        self.description = spec.get("description", "")
        self.error_pats = _compile(spec.get("error_patterns", []))
        self.warn_pats = _compile(spec.get("warn_patterns", []))
        self.tailers = [FileTailer(Path(p)) for p in spec.get("log_files", [])]
        self.callback = callback
        self.poll_interval = poll_interval
        self.lines: deque[dict[str, Any]] = deque(maxlen=2000)  # 近期行缓冲
        self._dedup: dict[tuple, float] = {}
        self._alert_ts: deque[float] = deque()
        self._throttled = False

    def poll_once(self) -> None:
        for tailer in self.tailers:
            try:
                lines = tailer.read_new()
            except Exception as e:  # 监听线程不能因单文件异常退出
                self.callback({"type": "log_error", "project": self.name,
                               "file": str(tailer.path), "message": str(e), "ts": _now()})
                continue
            for line in lines:
                self._handle_line(tailer, line)

    def _handle_line(self, tailer: FileTailer, line: str) -> None:
        rec = {"type": "line", "project": self.name, "file": tailer.path.name,
               "line": line, "ts": _now()}
        self.lines.append(rec)
        self.callback(rec)

        level = self._match(line)
        if level is None:
            return
        now = time.monotonic()
        key = (str(tailer.path), line.strip())
        if now - self._dedup.get(key, -1e9) < _DEDUP_WINDOW:
            return
        self._dedup[key] = now

        # 文件级节流
        while self._alert_ts and now - self._alert_ts[0] > _FILE_ALERT_WINDOW:
            self._alert_ts.popleft()
        if len(self._alert_ts) >= _FILE_ALERT_LIMIT:
            if not self._throttled:
                self._throttled = True
                self.callback({
                    "type": "alert", "project": self.name, "file": tailer.path.name,
                    "level": "error", "throttled": True,
                    "line": f"⚠ {tailer.path.name} 在 {_FILE_ALERT_WINDOW:.0f}s 内错误超过 {_FILE_ALERT_LIMIT} 条，后续告警暂节流",
                    "ts": _now(),
                })
            return
        self._alert_ts.append(now)
        self._throttled = False

        self.callback({
            "type": "alert", "project": self.name, "file": tailer.path.name,
            "line_no": tailer.line_no, "level": level, "line": line, "ts": _now(),
        })

    def _match(self, line: str) -> str | None:
        for p in self.error_pats:
            if p.search(line):
                return "error"
        for p in self.warn_pats:
            if p.search(line):
                return "warn"
        return None

    def tail(self, n: int = 100, file_name: str | None = None) -> list[dict[str, Any]]:
        items = [x for x in self.lines if file_name is None or x["file"] == file_name]
        return list(items[-n:])


class LogMonitor:
    """监听全部项目的后台线程。"""

    def __init__(self, monitor_conf: dict[str, Any], callback: EventCallback):
        self.poll_interval = float(monitor_conf.get("poll_interval", 0.5))
        self.projects = [
            ProjectMonitor(spec, callback, self.poll_interval)
            for spec in monitor_conf.get("projects", [])
        ]
        self._callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="logmon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            for proj in self.projects:
                try:
                    proj.poll_once()
                except Exception as e:
                    self._callback({"type": "log_error", "project": proj.name,
                                    "message": repr(e), "ts": _now()})
            self._stop.wait(self.poll_interval)

    # ---- 查询接口 ----
    def project_names(self) -> list[str]:
        return [p.name for p in self.projects]

    def add_project(self, spec: dict[str, Any]) -> ProjectMonitor:
        """运行时动态挂载新监听项目（能力进化用）。同名则替换。"""
        name = spec.get("name", "unnamed")
        self.projects = [p for p in self.projects if p.name != name]
        proj = ProjectMonitor(spec, self._callback, self.poll_interval)
        self.projects.append(proj)
        self._callback({"type": "logmon", "action": "project_added",
                        "project": name, "ts": _now()})
        return proj

    def get_project(self, name: str) -> ProjectMonitor | None:
        for p in self.projects:
            if p.name == name:
                return p
        return None

    def recent_lines(self, name: str, n: int = 100, file_name: str | None = None) -> list[dict[str, Any]]:
        proj = self.get_project(name)
        return proj.tail(n, file_name) if proj else []
