"""定时汇报：周期性汇总监听结果与系统状态，推送对话（可选 LLM 摘要 / QQ 转发）。"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

Notify = Callable[[dict[str, Any]], None]


class ReportScheduler:
    def __init__(self, report_conf: dict[str, Any], system: Any, logmon: Any,
                 alerts: deque, notify: Notify, provider: Any = None):
        self.conf = report_conf or {}
        self.system = system
        self.logmon = logmon
        self.alerts = alerts
        self._notify = notify
        self.provider = provider
        self._last_ts = time.time()
        self._line_counts: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    # ---------- 摘要（规则层，零成本） ----------
    def build_digest(self) -> str:
        now = time.time()
        since = self._last_ts
        self._last_ts = now

        window = [a for a in self.alerts if _ts_epoch(a.get("ts")) >= since]
        errors = [a for a in window if a.get("level") == "error" and not a.get("throttled")]
        warns = [a for a in window if a.get("level") == "warn"]

        def _group(items: list[dict]) -> dict[str, int]:
            out: dict[str, int] = {}
            for a in items:
                out[a.get("project", "?")] = out.get(a.get("project", "?"), 0) + 1
            return out

        st = self.system.poll()
        cpu_hist = [h["cpu"] for h in st.get("history", [])[-30:]] or [st["cpu"]["percent"]]
        mem = st["memory"]
        lines = [
            f"📊 SysAgent 定时汇报（{datetime.now().strftime('%H:%M')}，距上次 {int((now - since) // 60)} 分钟）",
            f"系统：CPU 当前 {st['cpu']['percent']}% / 均值 {sum(cpu_hist)/len(cpu_hist):.0f}%，"
            f"内存 {mem['percent']}%（可用 {mem['available'] // (1<<20)} MiB）",
        ]
        disks = [d for d in st.get("disks", []) if d.get("percent", 0) >= 80]
        if disks:
            lines.append("磁盘告警：" + "；".join(
                f"{d['mountpoint']} {d['percent']}%" for d in disks))

        if not window:
            lines.append("时段告警：无 ✅")
        else:
            lines.append(f"时段告警：ERROR {len(errors)} 条 / WARN {len(warns)} 条")
            for label, grp in (("ERROR", _group(errors)), ("WARN", _group(warns))):
                if grp:
                    lines.append(f"  {label} 分布：" + "，".join(f"{k}×{v}" for k, v in grp.items()))
            for a in errors[-3:]:
                line = (a.get("line") or "")[:120]
                lines.append(f"  · [{a.get('project')}] {a.get('file', '?')}: {line}")

        # 各项目新增行数
        proj_bits = []
        for name in self.logmon.project_names():
            proj = self.logmon.get_project(name)
            n = len(proj.lines) if proj else 0
            delta = n - self._line_counts.get(name, n)
            self._line_counts[name] = n
            errs = sum(1 for x in (proj.tail(200) if proj else [])
                       if "ERROR" in (x.get("line") or "").upper() or "Traceback" in (x.get("line") or ""))
            proj_bits.append(f"{name} +{delta} 行/近200行错误 {errs}")
        if proj_bits:
            lines.append("监听项目：" + "；".join(proj_bits))
        return "\n".join(lines)

    async def _emit_report(self) -> None:
        try:
            digest = self.build_digest()
        except Exception as e:
            self._notify({"type": "log_error", "project": "report",
                          "message": f"汇报生成失败: {e!r}", "ts": datetime.now().isoformat()})
            return
        text = digest
        if self.conf.get("llm_summary") and self.provider is not None:
            try:
                reply = await self.provider.chat([
                    {"role": "system", "content": "你是运维汇报助手。根据数据给出2-4条简短观察与建议，中文，不要重复罗列原始数据。"},
                    {"role": "user", "content": digest},
                ], None)
                if reply.content:
                    text = digest + "\n\n智能体观察：\n" + reply.content
            except Exception as e:
                text = digest + f"\n\n（LLM 摘要失败: {e}）"
        self._notify({"type": "chat", "role": "assistant", "content": text,
                      "source": "report", "report": True})

    async def run(self) -> None:
        interval = max(float(self.conf.get("interval_minutes", 30)) * 60, 60)
        while True:
            await asyncio.sleep(interval)
            if self.conf.get("enabled", True):
                await self._emit_report()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def _ts_epoch(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0
