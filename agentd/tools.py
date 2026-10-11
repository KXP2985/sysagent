"""工具注册、白名单校验与危险操作审批。

风险分级：
- 只读工具（status/log/read）→ 直接执行
- run_whitelisted_command 且 needs_approval=false → 直接执行
- run_whitelisted_command 且 needs_approval=true / start_project / stop_project
  → 返回 needs_approval，由 Web 端人工批准后携带 approval_id 重放
- 任何不在白名单里的命令 → 一律拒绝（LLM 无法自由拼接命令执行）
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import psutil

from .config import Config
from .logmon import LogMonitor
from .system import SystemCollector

Notify = Callable[[dict[str, Any]], None]

WRITE_TOOLS = {"run_whitelisted_command", "start_project", "stop_project", "scaffold_project"}
_OUTPUT_LIMIT = 8000
_CMD_TIMEOUT = 30.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class ApprovalManager:
    """待审批操作的登记与裁决。"""

    def __init__(self, notify: Notify):
        self._pending: dict[str, dict[str, Any]] = {}
        self._notify = notify

    def create(self, tool: str, args: dict[str, Any], description: str,
               source: str | None = None) -> str:
        aid = uuid.uuid4().hex[:12]
        item = {
            "id": aid, "tool": tool, "args": args, "description": description,
            "status": "pending", "created": _now(), "source": source,
        }
        self._pending[aid] = item
        self._notify({"type": "approval", "action": "added", "approval": item, "source": source})
        return aid
        return aid

    def list_pending(self) -> list[dict[str, Any]]:
        return [a for a in self._pending.values() if a["status"] == "pending"]

    def _set(self, aid: str, status: str) -> bool:
        item = self._pending.get(aid)
        if not item or item["status"] != "pending":
            return False
        item["status"] = status
        self._notify({"type": "approval", "action": status, "approval": item,
                      "source": item.get("source")})
        return True

    def approve(self, aid: str) -> bool:
        return self._set(aid, "approved")

    def reject(self, aid: str) -> bool:
        return self._set(aid, "rejected")

    def consume(self, aid: str) -> bool:
        """批准且未使用过 → 标记为 used 并放行。"""
        item = self._pending.get(aid)
        if not item or item["status"] != "approved":
            return False
        item["status"] = "used"
        self._notify({"type": "approval", "action": "used", "approval": item})
        return True

    def drop(self, aid: str) -> None:
        self._pending.pop(aid, None)


class ToolExecutor:
    def __init__(self, config: Config, system: SystemCollector,
                 logmon: LogMonitor, approvals: ApprovalManager, notify: Notify,
                 memory: Any = None, capabilities: Any = None):
        self.config = config
        self.system = system
        self.logmon = logmon
        self.approvals = approvals
        self.memory = memory
        self.capabilities = capabilities
        self._notify = notify
        self._started: dict[str, subprocess.Popen] = {}  # project -> Popen
        self.context_source: str | None = None  # 会话来源（qq:... / report），审批事件透传

    # ---------- schemas ----------
    def schemas(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {
                "name": "get_system_status",
                "description": "获取当前系统状态摘要（CPU/内存/磁盘/网络/Top进程/运行时长）",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            {"type": "function", "function": {
                "name": "list_monitored_projects",
                "description": "列出被监听的项目及其日志文件、近期错误情况",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            {"type": "function", "function": {
                "name": "read_project_log",
                "description": "读取某个被监听项目的最近日志行（定位问题时优先用它）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "项目名，见 list_monitored_projects"},
                        "lines": {"type": "integer", "description": "返回行数，默认 100"},
                        "file": {"type": "string", "description": "可选，只看指定日志文件名"},
                    },
                    "required": ["project"],
                },
            }},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "读取白名单目录内文本文件的末尾若干行（超出白名单目录会被拒绝）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "lines": {"type": "integer", "description": "末尾行数，默认 100"},
                    },
                    "required": ["path"],
                },
            }},
            {"type": "function", "function": {
                "name": "run_whitelisted_command",
                "description": "执行配置白名单中的命令（按 name 调用，不能自定义命令内容）",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "白名单命令的 name"}},
                    "required": ["name"],
                },
            }},
            {"type": "function", "function": {
                "name": "start_project",
                "description": "启动已配置 start_command 的项目（需人工审批）",
                "parameters": {
                    "type": "object",
                    "properties": {"project": {"type": "string"}},
                    "required": ["project"],
                },
            }},
            {"type": "function", "function": {
                "name": "stop_project",
                "description": "停止由本智能体启动的项目进程（需人工审批）",
                "parameters": {
                    "type": "object",
                    "properties": {"project": {"type": "string"}},
                    "required": ["project"],
                },
            }},
            {"type": "function", "function": {
                "name": "remember",
                "description": "把业务事实、故障经验、运维剧本沉淀到长期记忆（自我学习）。"
                               "解决完故障、搞清楚业务约定后应主动调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["fact", "lesson", "playbook"],
                                 "description": "fact=业务事实 lesson=故障经验 playbook=运维剧本"},
                        "title": {"type": "string", "description": "一句话标题"},
                        "content": {"type": "string", "description": "要点：现象/根因/方案/适用范围"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "检索标签"},
                    },
                    "required": ["kind", "title", "content"],
                },
            }},
            {"type": "function", "function": {
                "name": "recall",
                "description": "按话题检索长期记忆（业务事实/故障经验/运维剧本）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词"},
                        "kind": {"type": "string", "enum": ["fact", "lesson", "playbook"]},
                    },
                    "required": ["query"],
                },
            }},
            {"type": "function", "function": {
                "name": "propose_capability",
                "description": "为智能体增加新能力（自我进化）。whitelist_command/monitor_project "
                               "需人工审批后生效；playbook 直接入记忆。禁止一次提案多个危险命令。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": ["whitelist_command", "monitor_project", "playbook"]},
                        "payload": {
                            "type": "object",
                            "description": "whitelist_command: {name, command, needs_approval}; "
                                           "monitor_project: {name, log_files[], error_patterns[]?, "
                                           "warn_patterns[]?, description?, start_command?}; "
                                           "playbook: {title, content, tags?}",
                        },
                        "reason": {"type": "string", "description": "为什么需要这个能力"},
                    },
                    "required": ["kind", "payload"],
                },
            }},
            {"type": "function", "function": {
                "name": "list_capabilities",
                "description": "查看智能体当前的能力库：白名单命令、监听项目、已进化能力、待审批提案",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            {"type": "function", "function": {
                "name": "list_code_projects",
                "description": "扫描允许目录下的代码项目（含项目类型与语言），用于学习代码风格",
                "parameters": {
                    "type": "object",
                    "properties": {"root": {"type": "string",
                                            "description": "可选，只扫某个扫描根目录（默认全部）"}},
                    "required": [],
                },
            }},
            {"type": "function", "function": {
                "name": "analyze_code_project",
                "description": "读取代码项目的关键文件（README/依赖/入口/样例源码），提炼风格画像并存入记忆",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "项目路径，见 list_code_projects"}},
                    "required": ["path"],
                },
            }},
            {"type": "function", "function": {
                "name": "scaffold_project",
                "description": "在沙箱 workspace 内按风格生成项目脚手架（文件写入需人工审批）。"
                               "只能写工作区内，路径含 .. 会被拒绝。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "项目名（字母数字_-）"},
                        "description": {"type": "string", "description": "项目做什么的"},
                        "files": {
                            "type": "array",
                            "description": "文件列表 [{path, content}]，path 为相对路径",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["path", "content"],
                            },
                        },
                    },
                    "required": ["name", "files"],
                },
            }},
        ]

    # ---------- dispatch ----------
    async def execute(self, name: str, args: dict[str, Any],
                      approval_id: str | None = None) -> dict[str, Any]:
        if name not in {s["function"]["name"] for s in self.schemas()}:
            return {"status": "error", "output": f"未知工具: {name}"}

        if name in WRITE_TOOLS:
            gate = self._gate(name, args, approval_id)
            if gate is not None:
                return gate

        try:
            return await self._dispatch(name, args or {})
        except Exception as e:  # 工具异常不能击穿 Agent 循环
            return {"status": "error", "output": f"工具执行异常: {e!r}"}

    def _gate(self, name: str, args: dict, approval_id: str | None) -> dict[str, Any] | None:
        """返回 None 表示放行；否则返回 needs_approval / error 结果。"""
        if approval_id:
            if self.approvals.consume(approval_id):
                return None
            return {"status": "error", "output": "审批无效、已被使用或未获批准"}

        desc = self._describe(name, args)
        if name == "run_whitelisted_command":
            entry = self._find_command(str(args.get("name", "")))
            if entry is None:
                available = ", ".join(c.get("name", "?") for c in self.config.whitelist_commands) or "(空)"
                return {"status": "error",
                        "output": f"命令不在白名单中，已拒绝。可用白名单命令: {available}"}
            if not entry.get("needs_approval"):
                return None  # 白名单声明为免审批
        aid = self.approvals.create(name, dict(args), desc, source=self.context_source)
        return {"status": "needs_approval", "approval_id": aid, "description": desc}

    def _describe(self, name: str, args: dict) -> str:
        if name == "run_whitelisted_command":
            entry = self._find_command(str(args.get("name", "")))
            cmd = entry.get("command", "?") if entry else str(args)
            return f"执行白名单命令「{args.get('name')}」: {cmd}"
        if name == "start_project":
            spec = self._find_project(str(args.get("project", "")))
            return f"启动项目「{args.get('project')}」: {spec.get('start_command') if spec else '?'}"
        if name == "stop_project":
            pid = self._started.get(str(args.get("project")))
            return f"停止项目「{args.get('project')}」进程 (pid={pid.pid if pid else '?'})"
        if name == "scaffold_project":
            files = args.get("files") or []
            return (f"在沙箱中创建项目「{args.get('name')}」脚手架"
                    f"（{len(files)} 个文件，工作区 {self._code_conf().get('workspace_dir', './workspace')}）")
        return f"{name} {args}"

    def _find_command(self, name: str) -> dict | None:
        for c in self.config.whitelist_commands:
            if c.get("name") == name:
                return c
        return None

    def _find_project(self, name: str) -> dict | None:
        for p in self.config.projects:
            if p.get("name") == name:
                return p
        return None

    # ---------- handlers ----------
    async def _dispatch(self, name: str, args: dict) -> dict[str, Any]:
        if name == "get_system_status":
            return {"status": "ok", "output": self.system.agent_summary()}
        if name == "list_monitored_projects":
            return {"status": "ok", "output": self._list_projects()}
        if name == "read_project_log":
            return {"status": "ok", "output": self._read_project_log(args)}
        if name == "read_file":
            return self._read_file(args)
        if name == "run_whitelisted_command":
            return await self._run_command(args)
        if name == "start_project":
            return await self._start_project(args)
        if name == "stop_project":
            return self._stop_project(args)
        if name == "remember":
            return self._remember(args)
        if name == "recall":
            return self._recall(args)
        if name == "propose_capability":
            return self._propose_capability(args)
        if name == "list_capabilities":
            return self._list_capabilities()
        if name == "list_code_projects":
            return self._list_code_projects(args)
        if name == "analyze_code_project":
            return self._analyze_code_project(args)
        if name == "scaffold_project":
            return self._scaffold_project(args)
        return {"status": "error", "output": f"未知工具: {name}"}

    # ---------- 代码学习（受限） ----------
    def _code_conf(self) -> dict[str, Any]:
        return self.config.data.get("code") or {}

    def _list_code_projects(self, args: dict) -> dict[str, Any]:
        from . import codescan
        scan_dirs = self._code_conf().get("scan_dirs") or []
        if not scan_dirs:
            return {"status": "error",
                    "output": "未配置 code.scan_dirs，无法扫描代码项目。请在 config.yaml 配置允许学习的代码根目录。"}
        roots = scan_dirs
        if args.get("root"):
            roots = [str(args["root"])]
        projects = codescan.list_projects(roots)
        if not projects:
            return {"status": "ok", "output": f"目录 {roots} 下未发现代码项目（深度≤3）"}
        lines = ["发现的代码项目："]
        for p in projects:
            lines.append(f"- {p['name']} ({', '.join(p['languages']) or '?'}) 标记: {','.join(p['markers'])}\n  {p['path']}")
        return {"status": "ok", "output": "\n".join(lines)}

    def _analyze_code_project(self, args: dict) -> dict[str, Any]:
        from . import codescan
        scan_dirs = self._code_conf().get("scan_dirs") or []
        if not scan_dirs:
            return {"status": "error", "output": "未配置 code.scan_dirs"}
        info = codescan.analyze_project(str(args.get("path", "")), scan_dirs)
        if info.get("error"):
            return {"status": "error", "output": info["error"]}
        # 自动存风格指纹进记忆（自我学习）
        if self.memory is not None:
            langs = ", ".join(info.get("languages") or []) or "未知"
            self.memory.add(
                "fact", f"代码风格画像：{info['project']}",
                f"语言: {langs}；构建标记: {','.join(info.get('markers') or [])}；"
                f"入口/配置样例: {', '.join((info.get('style_files') or {}).keys())}；"
                f"路径: {info['path']}。生成同风格代码时请参考其目录结构、命名与依赖写法。",
                tags=["code-style", info["project"]], source="code-scan",
            )
        lines = [f"项目: {info['project']} @ {info['path']}",
                 f"语言: {', '.join(info.get('languages') or [])}",
                 f"标记文件: {', '.join(info.get('markers') or [])}",
                 "", "目录树:", info.get("tree", ""), "", "—— 关键文件 ——"]
        for fn, text in (info.get("style_files") or {}).items():
            lines.append(f"\n### {fn}\n{text[:1500]}")
        for fn, text in (info.get("sample_sources") or {}).items():
            lines.append(f"\n### {fn}\n{text[:1500]}")
        return {"status": "ok", "output": "\n".join(lines)[:12000]}

    def _scaffold_project(self, args: dict) -> dict[str, Any]:
        from . import codescan
        ws = self._code_conf().get("workspace_dir") or "./workspace"
        name = str(args.get("name", ""))
        files = args.get("files") or []
        result = codescan.scaffold_files(ws, name, files)
        if result.get("error"):
            return {"status": "error", "output": result["error"]}
        return {"status": "ok",
                "output": f"脚手架已写入 {result['path']}（{len(result['written'])} 个文件）: "
                          + ", ".join(result["written"][:20])}

    # ---------- 记忆与进化 ----------
    def _remember(self, args: dict) -> dict[str, Any]:
        if self.memory is None:
            return {"status": "error", "output": "记忆模块未启用"}
        try:
            entry = self.memory.add(
                str(args.get("kind", "fact")),
                str(args.get("title", "")),
                str(args.get("content", "")),
                tags=[str(t) for t in (args.get("tags") or [])],
                source="agent",
            )
        except ValueError as e:
            return {"status": "error", "output": str(e)}
        return {"status": "ok",
                "output": f"已记住（{entry['kind']} id={entry['id']}）：{entry['title']}"}

    def _recall(self, args: dict) -> dict[str, Any]:
        if self.memory is None:
            return {"status": "error", "output": "记忆模块未启用"}
        query = str(args.get("query", ""))
        kind = args.get("kind")
        hits = self.memory.search(query, kind=str(kind) if kind else None, limit=8)
        if not hits:
            return {"status": "ok", "output": f"记忆中没有与「{query}」相关的内容。可在搞清楚后调用 remember 沉淀。"}
        lines = []
        for e in hits:
            lines.append(f"[{e['kind']}] {e['title']}（id={e['id']}）\n  {e['content'][:300]}")
        return {"status": "ok", "output": "\n".join(lines)}

    def _propose_capability(self, args: dict) -> dict[str, Any]:
        if self.capabilities is None:
            return {"status": "error", "output": "能力进化模块未启用"}
        payload = args.get("payload") or {}
        if not isinstance(payload, dict):
            return {"status": "error", "output": "payload 必须是对象"}
        result = self.capabilities.propose(
            str(args.get("kind", "")), payload, str(args.get("reason", "")))
        if result.get("status") == "needs_approval":
            return {"status": "ok",
                    "output": f"能力提案已提交（id={result['approval_id']}）：{result['description']}\n"
                              f"用户会在控制台审批，批准后立即生效；本次回答可继续，不必等待。"}
        return result

    def _list_capabilities(self) -> dict[str, Any]:
        cmds = self.config.whitelist_commands
        lines = ["白名单命令（可直接调用 run_whitelisted_command）:"]
        if cmds:
            for c in cmds:
                flag = "直接执行" if not c.get("needs_approval") else "需审批"
                lines.append(f"  - {c.get('name')}: {c.get('command')} [{flag}]")
        else:
            lines.append("  （空）")
        lines.append("监听项目:")
        for p in self.config.projects:
            files = ", ".join(Path(str(f)).name for f in p.get("log_files", []))
            lines.append(f"  - {p.get('name')}: {p.get('description', '')} ({files})")
        if self.capabilities is not None:
            evolved = self.capabilities.list_applied()
            lines.append("已进化能力:")
            if evolved:
                for a in evolved:
                    label = a.get("command") or a.get("name")
                    lines.append(f"  - [{a.get('kind')}] {a.get('name')}: {label}")
            else:
                lines.append("  （尚无，可通过 propose_capability 增加）")
            pending = self.capabilities.list_pending()
            if pending:
                lines.append("待审批能力提案:")
                for it in pending:
                    lines.append(f"  - {it.get('id')}: {it.get('description')}")
        return {"status": "ok", "output": "\n".join(lines)}

    def _list_projects(self) -> str:
        if not self.config.projects:
            return "未配置任何被监听项目（config.yaml -> monitor.projects）"
        lines = []
        for p in self.config.projects:
            name = p.get("name", "?")
            mon = self.logmon.get_project(name)
            recent = mon.tail(200) if mon else []
            errs = [r for r in recent if "ERROR" in r["line"].upper()
                    or "Traceback" in r["line"] or "FATAL" in r["line"].upper()]
            files = [Path(f).name for f in p.get("log_files", [])]
            lines.append(
                f"- {name}: {p.get('description', '')} | 日志文件: {', '.join(files) or '无'}"
                f" | 近期日志 {len(recent)} 行，其中疑似错误 {len(errs)} 行"
            )
            if errs:
                lines.append(f"  最近错误: {errs[-1]['line'][:160]}")
        return "\n".join(lines)

    def _read_project_log(self, args: dict) -> str:
        project = str(args.get("project", ""))
        n = min(int(args.get("lines", 100) or 100), 500)
        file_name = args.get("file")
        if self.logmon.get_project(project) is None:
            return (f"项目不存在: {project}。已配置项目: "
                    + ", ".join(self.logmon.project_names()))
        rows = self.logmon.recent_lines(project, n, str(file_name) if file_name else None)
        if not rows:
            return f"项目 {project} 暂无日志行（文件可能尚未产生内容）"
        return "\n".join(f"[{r['ts']}] {r['file']}: {r['line']}" for r in rows)

    def _read_file(self, args: dict) -> dict[str, Any]:
        raw = str(args.get("path", "")).strip()
        n = min(int(args.get("lines", 100) or 100), 1000)
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError) as e:
            return {"status": "error", "output": f"路径无效: {e}"}

        allowed = any(self._is_under(path, d) for d in self.config.read_dirs)
        if not allowed:
            dirs = ", ".join(self.config.read_dirs) or "(未配置)"
            return {"status": "error",
                    "output": f"拒绝读取: {path} 不在白名单目录内。允许目录: {dirs}"}
        if not path.is_file():
            return {"status": "error", "output": f"文件不存在: {path}"}
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > 512 * 1024:
                    f.seek(size - 512 * 1024)
                data = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            return {"status": "error", "output": f"读取失败: {e}"}
        lines = [ln for ln in data.splitlines() if ln][-n:]
        head = f"(文件共 {size} 字节，显示末尾 {len(lines)} 行) {path}\n" if size > 512 * 1024 else f"{path}\n"
        return {"status": "ok", "output": head + "\n".join(lines)}

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    async def _run_command(self, args: dict) -> dict[str, Any]:
        entry = self._find_command(str(args.get("name", "")))
        if entry is None:
            return {"status": "error", "output": "白名单命令不存在"}
        command = str(entry.get("command", ""))
        timeout = float(entry.get("timeout", _CMD_TIMEOUT))
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"status": "error", "output": f"命令超时 ({timeout}s): {command}"}
        text = (out or b"").decode("utf-8", errors="replace")
        if len(text) > _OUTPUT_LIMIT:
            text = text[:_OUTPUT_LIMIT] + f"\n...（输出已截断，共 {len(text)} 字符）"
        return {"status": "ok",
                "output": f"$ {command}\n退出码 {proc.returncode}\n{text or '(无输出)'}"}

    async def _start_project(self, args: dict) -> dict[str, Any]:
        name = str(args.get("project", ""))
        spec = self._find_project(name)
        if spec is None:
            return {"status": "error",
                    "output": f"项目未配置: {name}。可用: "
                              + (", ".join(p.get('name', '?') for p in self.config.projects) or "(无)")}
        cmd = str(spec.get("start_command") or "").strip()
        if not cmd:
            return {"status": "error", "output": f"项目 {name} 未配置 start_command"}
        if name in self._started and self._started[name].poll() is None:
            return {"status": "error", "output": f"项目 {name} 已在运行 (pid={self._started[name].pid})"}

        workdir = spec.get("workdir") or None
        log_target = None
        log_path = None
        if spec.get("log_files"):
            log_path = Path(spec["log_files"][0])
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_target = open(log_path, "ab")
            except OSError:
                log_target = None
        try:
            popen = subprocess.Popen(
                command=cmd,
                shell=True,
                cwd=workdir,
                stdout=log_target or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_target else subprocess.DEVNULL,
            )
        except OSError as e:
            return {"status": "error", "output": f"启动失败: {e}"}
        finally:
            if log_target:
                log_target.close()
        self._started[name] = popen
        return {"status": "ok",
                "output": f"项目 {name} 已启动 pid={popen.pid}"
                          + (f"，控制台输出重定向到 {log_path}" if log_path else "")}

    def _stop_project(self, args: dict) -> dict[str, Any]:
        name = str(args.get("project", ""))
        proc = self._started.get(name)
        if proc is None or proc.poll() is not None:
            return {"status": "error",
                    "output": f"项目 {name} 不在本智能体管理的运行列表中。"
                              f"（智能体只能停止自己启动的进程；外部启动的进程请用白名单命令处理）"}
        try:
            p = psutil.Process(proc.pid)
            p.terminate()
            gone, alive = psutil.wait_procs([p], timeout=5)
            if alive:
                p.kill()
        except psutil.Error as e:
            return {"status": "error", "output": f"停止失败: {e}"}
        self._started.pop(name, None)
        return {"status": "ok", "output": f"项目 {name} 已停止 (pid={proc.pid})"}
