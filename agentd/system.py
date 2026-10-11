"""跨平台系统状态采集（Windows / Linux），基于 psutil。"""
from __future__ import annotations

import platform
import socket
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import psutil

# cpu_percent 首次调用返回 0，先预热
psutil.cpu_percent(interval=None)

_SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "overlay", "squashfs", "rpc_pipefs", "nsfs", "bpf", "pstore",
    "securityfs", "debugfs", "tracefs", "hugetlbfs", "mqueue", "fusectl",
    "configfs", "binfmt_misc", "autofs", "efivarfs",
}


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _disk_partitions() -> list[dict[str, Any]]:
    out = []
    for p in psutil.disk_partitions(all=False):
        if p.fstype.lower() in _SKIP_FSTYPES:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (OSError, PermissionError):
            continue  # Windows 光驱无介质等
        out.append({
            "device": p.device,
            "mountpoint": p.mountpoint,
            "fstype": p.fstype,
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "percent": u.percent,
        })
    return out


def _top_processes(limit: int = 10) -> list[dict[str, Any]]:
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_percent", "create_time"]):
        try:
            info = p.info
            name = info["name"] or "?"
            if info["pid"] == 0 or name == "System Idle Process":
                continue
            cpu = p.cpu_percent(interval=None)  # 首次为 0，之后基于上次调用
            procs.append({
                "pid": info["pid"],
                "name": name,
                "cpu_percent": round(cpu, 1),
                "memory_percent": round(info["memory_percent"] or 0.0, 1),
                "started": datetime.fromtimestamp(
                    info["create_time"] or 0
                ).isoformat(timespec="seconds") if info["create_time"] else None,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    procs.sort(key=lambda x: (x["cpu_percent"], x["memory_percent"]), reverse=True)
    return procs[:limit]


class SystemCollector:
    """周期采集状态并保留历史，供仪表盘画趋势图。"""

    def __init__(self, history_len: int = 180):
        self._prev_net = psutil.net_io_counters()
        self._prev_net_ts = time.monotonic()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_len)
        self.boot_time = psutil.boot_time()

    def poll(self) -> dict[str, Any]:
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(now - self._prev_net_ts, 1e-6)
        rx_rate = max((net.bytes_recv - self._prev_net.bytes_recv) / dt, 0)
        tx_rate = max((net.bytes_sent - self._prev_net.bytes_sent) / dt, 0)
        self._prev_net, self._prev_net_ts = net, now

        vm = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=None)
        sample = {
            "cpu": cpu_percent,
            "mem": vm.percent,
            "net_rx": rx_rate,
            "net_tx": tx_rate,
            "t": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._history.append(sample)

        try:
            load_avg = list(psutil.getloadavg())
        except (AttributeError, OSError):
            load_avg = None  # Windows 无 loadavg

        return {
            "time": _ts(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "hostname": socket.gethostname(),
                "python": platform.python_version(),
            },
            "uptime_seconds": int(time.time() - self.boot_time),
            "cpu": {
                "percent": cpu_percent,
                "cores_physical": psutil.cpu_count(logical=False),
                "cores_logical": psutil.cpu_count(logical=True),
                "per_core": psutil.cpu_percent(interval=None, percpu=True),
                "load_avg": load_avg,
            },
            "memory": {
                "total": vm.total,
                "available": vm.available,
                "used": vm.used,
                "percent": vm.percent,
                "swap_percent": psutil.swap_memory().percent,
            },
            "disks": _disk_partitions(),
            "network": {
                "bytes_sent_total": net.bytes_sent,
                "bytes_recv_total": net.bytes_recv,
                "send_rate": tx_rate,
                "recv_rate": rx_rate,
            },
            "process_count": len(psutil.pids()),
            "top_processes": _top_processes(),
            "history": list(self._history),
        }

    def agent_summary(self) -> str:
        """给 LLM 的紧凑状态摘要。"""
        s = self.poll()
        mem = s["memory"]
        lines = [
            f"系统: {s['platform']['system']} {s['platform']['release']} @ {s['platform']['hostname']}",
            f"运行时长: {s['uptime_seconds'] // 3600}h{(s['uptime_seconds'] % 3600) // 60}m",
            f"CPU: {s['cpu']['percent']}% (逻辑核 {s['cpu']['cores_logical']})",
            f"内存: {mem['percent']}% (可用 {mem['available'] // (1 << 20)} MiB / 共 {mem['total'] // (1 << 20)} MiB)，Swap {mem['swap_percent']}%",
        ]
        for d in s["disks"][:6]:
            lines.append(f"磁盘 {d['mountpoint']}: {d['percent']}% (余 {d['free'] // (1 << 30)} GiB)")
        lines.append(f"网络: ↓{s['network']['recv_rate'] / 1024:.1f} KiB/s ↑{s['network']['send_rate'] / 1024:.1f} KiB/s")
        tops = ", ".join(
            f"{p['name']}({p['pid']}) cpu={p['cpu_percent']}% mem={p['memory_percent']}%"
            for p in s["top_processes"][:6]
        )
        lines.append(f"资源占用 Top 进程: {tops}")
        return "\n".join(lines)
