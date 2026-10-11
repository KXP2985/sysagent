"""FastAPI 服务端：REST 查询 + WebSocket 实时通道 + 静态控制台。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent
from .capabilities import CapabilityManager
from .config import Config
from .llm import LLMError
from .logmon import LogMonitor
from .memory import MemoryStore
from .models import ModelRegistry, ping_provider
from .paths import static_dir
from .qqchannel import QQChannel
from .reporting import ReportScheduler
from .system import SystemCollector
from .tools import ApprovalManager, ToolExecutor

# 便携模式从 _MEIPASS/static 读界面，源码模式读项目 static/
STATIC_DIR = static_dir()


class Broadcaster:
    """线程安全的事件广播：日志线程 / Agent / 状态任务 → 所有 WS 客户端。"""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)

    def emit(self, event: dict[str, Any]) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._put, event)

    def _put(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 极端洪峰下丢弃旧事件，保活服务

    async def pump(self) -> None:
        while True:
            event = await self.queue.get()
            dead = []
            data = json.dumps(event, ensure_ascii=False, default=str)
            for ws in list(self.clients):
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


class AppState:
    def __init__(self, config: Config):
        self.config = config
        self.broadcaster = Broadcaster()
        self.collector = SystemCollector()
        self.alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self.started_at = time.time()
        self.qq: QQChannel | None = None
        self.qq_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)

    # 事件入口：日志线程回调 & Agent/审批通知都走这里
    def notify(self, event: dict[str, Any]) -> None:
        if event.get("type") == "alert":
            self.alerts.append(event)
        self.broadcaster.emit(event)
        if self.qq is not None and self.broadcaster.loop is not None:
            self.broadcaster.loop.call_soon_threadsafe(self._qq_put, event)

    def _qq_put(self, event: dict[str, Any]) -> None:
        try:
            self.qq_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def create_app(config: Config) -> FastAPI:
    state = AppState(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        b = state.broadcaster
        b.loop = asyncio.get_running_loop()

        logmon = LogMonitor(config.data["monitor"], state.notify)
        memory = MemoryStore(Path(config.data.get("data_dir", "./data")).expanduser() / "memory.jsonl")
        capabilities = CapabilityManager(config, memory, state.notify, logmon.add_project)
        approvals = ApprovalManager(state.notify)
        executor = ToolExecutor(config, state.collector, logmon, approvals, state.notify,
                                memory=memory, capabilities=capabilities)
        registry = ModelRegistry(config)
        provider = registry.create_provider()  # 以注册表 active 为准（覆盖 config 种子）
        agent = Agent(config, executor, provider, state.notify,
                      hostname=_hostname(), os_name=_os_name())

        app.state.state = state
        app.state.provider = provider
        app.state.registry = registry
        app.state.logmon = logmon
        app.state.approvals = approvals
        app.state.executor = executor
        app.state.agent = agent
        app.state.memory = memory
        app.state.capabilities = capabilities

        # 定时汇报
        reporter = ReportScheduler(config.data.get("report") or {}, state.collector,
                                   logmon, state.alerts, state.notify, provider=provider)
        app.state.reporter = reporter

        # QQ 通道
        qq = QQChannel(config.data.get("qq") or {}, state.notify,
                       handle_user=lambda text, source=None: agent.handle_user(text, source=source),
                       decide_approval=lambda aid, ok: _qq_decide(app, aid, ok),
                       get_pending=approvals.list_pending)
        state.qq = qq
        app.state.qq = qq

        state.collector.poll()  # 预热
        logmon.start()
        pump_task = asyncio.create_task(b.pump())
        metrics_task = asyncio.create_task(_metrics_loop(state))
        report_task = asyncio.create_task(reporter.run())
        qq_pump_task = asyncio.create_task(_qq_pump(state))
        try:
            yield
        finally:
            for t in (metrics_task, pump_task, report_task, qq_pump_task):
                t.cancel()
            logmon.stop()
            await provider.aclose()
            await qq.aclose()

    async def _qq_decide(app: FastAPI, approval_id: str, approved: bool) -> None:
        approvals: ApprovalManager = app.state.approvals
        agent: Agent = app.state.agent
        ok = approvals.approve(approval_id) if approved else approvals.reject(approval_id)
        if ok:
            await agent.resolve_approval(approval_id, approved=approved)

    async def _qq_pump(state: AppState) -> None:
        while True:
            event = await state.qq_queue.get()
            if state.qq:
                try:
                    await state.qq.forward_agent_event(event)
                except Exception:
                    pass

    app = FastAPI(title="SysAgent", lifespan=lifespan)

    # ---------- REST ----------
    @app.get("/api/health")
    async def health():
        return {"ok": True, "uptime": time.time() - state.started_at}

    @app.get("/api/status")
    async def status():
        return state.collector.poll()

    @app.get("/api/config")
    async def get_config():
        return config.public_snapshot()

    @app.get("/api/alerts")
    async def get_alerts(limit: int = 100):
        return list(state.alerts)[-limit:]

    @app.get("/api/projects/{name}/lines")
    async def project_lines(name: str, n: int = 100, file: str | None = None):
        logmon: LogMonitor = app.state.logmon
        if logmon.get_project(name) is None:
            raise HTTPException(404, f"项目不存在: {name}")
        return logmon.recent_lines(name, min(n, 1000), file)

    @app.post("/api/approvals/{approval_id}/{decision}")
    async def decide(approval_id: str, decision: str):
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "decision 必须是 approve 或 reject")
        approvals: ApprovalManager = app.state.approvals
        agent: Agent = app.state.agent
        ok = approvals.approve(approval_id) if decision == "approve" else approvals.reject(approval_id)
        if not ok:
            raise HTTPException(409, "审批不存在或已处理")
        await agent.resolve_approval(approval_id, approved=(decision == "approve"))
        return {"ok": True}

    # ---------- 记忆 / 能力进化 ----------
    @app.get("/api/memory")
    async def get_memory(kind: str | None = None, q: str | None = None, limit: int = 100):
        mem: MemoryStore = app.state.memory
        if q:
            return mem.search(q, kind=kind, limit=min(limit, 50))
        return mem.list(kind=kind, limit=min(limit, 200))

    @app.delete("/api/memory/{entry_id}")
    async def delete_memory(entry_id: str):
        mem: MemoryStore = app.state.memory
        if not mem.remove(entry_id):
            raise HTTPException(404, "记忆条目不存在")
        return {"ok": True}

    @app.get("/api/capabilities")
    async def get_capabilities():
        caps: CapabilityManager = app.state.capabilities
        return {
            "pending": caps.list_pending(),
            "applied": caps.list_applied(),
            "memory_stats": app.state.memory.stats(),
        }

    @app.post("/api/capabilities/{cap_id}/{decision}")
    async def decide_capability(cap_id: str, decision: str):
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "decision 必须是 approve 或 reject")
        caps: CapabilityManager = app.state.capabilities
        if not caps.decide(cap_id, approved=(decision == "approve")):
            raise HTTPException(409, "提案不存在或已处理")
        return {"ok": True}

    # ---------- 模型管理（云端模型 + 默认切换） ----------
    @app.get("/api/llm/providers")
    async def llm_list():
        registry: ModelRegistry = app.state.registry
        return {"active": registry.active_id, "providers": registry.list_public()}

    @app.post("/api/llm/providers")
    async def llm_add(body: dict[str, Any]):
        registry: ModelRegistry = app.state.registry
        try:
            entry = registry.add(
                name=body.get("name", ""), base_url=body.get("base_url", ""),
                api_key=body.get("api_key", ""), model=body.get("model", ""),
                temperature=body.get("temperature", 0.3), timeout=body.get("timeout", 60),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": entry["id"], "provider": registry.list_public()[-1]}

    @app.delete("/api/llm/providers/{pid}")
    async def llm_remove(pid: str):
        registry: ModelRegistry = app.state.registry
        if not registry.remove(pid):
            raise HTTPException(400, "删除失败（不存在或为内置保护项）")
        # 若删除的是当前默认，切换到新的 active
        await _swap_llm(app, registry.active_id)
        return {"ok": True, "active": registry.active_id}

    @app.post("/api/llm/providers/{pid}/active")
    async def llm_set_active(pid: str):
        registry: ModelRegistry = app.state.registry
        try:
            entry = registry.set_active(pid)
        except KeyError:
            raise HTTPException(404, "模型不存在")
        await _swap_llm(app, pid)
        return {"ok": True, "active": pid, "name": entry.get("name"),
                "model": entry.get("model")}

    @app.post("/api/llm/test")
    async def llm_test(body: dict[str, Any]):
        """连通性测试：传 id 测已有配置，或直接传完整配置测未保存的表单。"""
        registry: ModelRegistry = app.state.registry
        pid = body.get("id")
        if pid:
            entry = registry.get(str(pid))
            if not entry:
                raise HTTPException(404, "模型不存在")
            kind = entry["kind"]
            conf = {"base_url": entry.get("base_url"), "api_key": entry.get("api_key"),
                    "model": entry.get("model"), "temperature": entry.get("temperature", 0.3),
                    "timeout": entry.get("timeout", 60)}
        else:
            kind = "ollama" if body.get("kind") == "ollama" else "openai_compatible"
            conf = {"base_url": body.get("base_url"), "api_key": body.get("api_key", ""),
                    "model": body.get("model"), "temperature": body.get("temperature", 0.3),
                    "timeout": float(body.get("timeout", 60))}
            if not conf.get("base_url") or not conf.get("model"):
                raise HTTPException(400, "base_url 与 model 必填")
        try:
            content = await ping_provider(kind, conf)
        except LLMError as e:
            return {"ok": False, "error": str(e)[:300]}
        return {"ok": True, "reply": content}

    async def _swap_llm(app: FastAPI, pid: str) -> None:
        """运行时热切换 Agent / 定时汇报使用的模型。"""
        registry: ModelRegistry = app.state.registry
        try:
            new_provider = registry.create_provider(pid)
        except LLMError as e:
            raise HTTPException(400, f"切换失败: {e}")
        app.state.provider = new_provider
        app.state.agent.provider = new_provider
        reporter = getattr(app.state, "reporter", None)
        if reporter is not None:
            reporter.provider = new_provider
        state.broadcaster.emit({
            "type": "llm_switched", "active": pid,
            "name": (registry.get(pid) or {}).get("name"),
            "model": (registry.get(pid) or {}).get("model"),
        })

    # ---------- QQ OneBot 上报入口 ----------
    @app.post("/qq/onebot")
    async def qq_onebot(request: Request):
        qq: QQChannel = app.state.qq
        if qq is None or not qq.enabled:
            raise HTTPException(404, "QQ 通道未启用")
        token = request.query_params.get("token") or request.headers.get("X-OneBot-Token")
        if not qq.check_webhook_token(token):
            raise HTTPException(403, "token 校验失败")
        try:
            event = await request.json()
        except Exception:
            raise HTTPException(400, "请求体必须是 JSON")
        result = await qq.handle_event(event if isinstance(event, dict) else {})
        return {"ok": result.get("ok", True), "detail": result}

    # ---------- WebSocket ----------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        state.broadcaster.clients.add(ws)
        try:
            agent: Agent = app.state.agent
            approvals: ApprovalManager = app.state.approvals
            logmon: LogMonitor = app.state.logmon
            memory: MemoryStore = app.state.memory
            caps: CapabilityManager = app.state.capabilities

            await ws.send_text(json.dumps({
                "type": "hello",
                "status": state.collector.poll(),
                "config": config.public_snapshot(),
                "pending_approvals": approvals.list_pending(),
                "pending_capabilities": caps.list_pending(),
                "applied_capabilities": caps.list_applied(),
                "memory_stats": memory.stats(),
                "projects": logmon.project_names(),
                "alerts": list(state.alerts)[-100:],
                "history": _chat_history(agent),
                "server_time": time.time(),
            }, ensure_ascii=False, default=str))

            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "chat":
                    text = str(msg.get("text", "")).strip()
                    if text:
                        # 放到任务里执行，避免阻塞 WS 收包循环
                        asyncio.create_task(agent.handle_user(text))
                elif mtype == "approve":
                    await _decide_ws(app, ws, str(msg.get("id", "")), True)
                elif mtype == "reject":
                    await _decide_ws(app, ws, str(msg.get("id", "")), False)
                elif mtype == "cap_approve":
                    caps.decide(str(msg.get("id", "")), approved=True)
                elif mtype == "cap_reject":
                    caps.decide(str(msg.get("id", "")), approved=False)
        except WebSocketDisconnect:
            pass
        finally:
            state.broadcaster.clients.discard(ws)

    async def _decide_ws(app: FastAPI, ws: WebSocket, approval_id: str, approved: bool) -> None:
        approvals: ApprovalManager = app.state.approvals
        agent: Agent = app.state.agent
        ok = approvals.approve(approval_id) if approved else approvals.reject(approval_id)
        if not ok:
            await ws.send_text(json.dumps(
                {"type": "chat_error", "content": "审批不存在或已处理"}, ensure_ascii=False))
            return
        await agent.resolve_approval(approval_id, approved=approved)

    # ---------- 静态文件 ----------
    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


async def _metrics_loop(state: AppState) -> None:
    """每 2s 采样轻量指标推给前端画趋势。"""
    while True:
        try:
            s = state.collector.poll()
            cpu, mem, net = s["cpu"], s["memory"], s["network"]
            state.broadcaster.emit({
                "type": "metrics", "t": s["time"],
                "cpu": cpu["percent"], "mem": mem["percent"],
                "swap": mem["swap_percent"],
                "net_rx": net["recv_rate"], "net_tx": net["send_rate"],
                "disks": [{"mountpoint": d["mountpoint"], "percent": d["percent"]}
                          for d in s["disks"]],
            })
        except Exception:
            pass
        await asyncio.sleep(2)


def _chat_history(agent: Agent) -> list[dict[str, Any]]:
    """给前端重放的对话（仅用户/助手可见文本）。"""
    out = []
    for m in agent.history:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _hostname() -> str:
    import socket
    return socket.gethostname()


def _os_name() -> str:
    import platform
    return f"{platform.system()} {platform.release()}"


def run(config: Config) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台防乱码
    except Exception:
        pass
    app = create_app(config)
    print(f"SysAgent 控制台: http://{config.host}:{config.port}/", flush=True)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
