"""长期记忆：业务事实 / 故障经验 / 运维剧本。

JSONL 持久化（data/memory.jsonl），每行一条：
  {"id", "kind": fact|lesson|playbook, "title", "content", "tags", "created", "updated", "source"}

检索：查询分词后与 title/content/tags 做重叠打分（中文按二元组、英文按词），
足够支撑「把相关经验注入提示词」这类轻量学习场景，无需向量库。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

KINDS = ("fact", "lesson", "playbook")
KIND_LABELS = {"fact": "业务事实", "lesson": "故障经验", "playbook": "运维剧本"}

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(text: str) -> list[str]:
    """英文按词、中文按连续段的二元组切分。"""
    out: list[str] = []
    text = (text or "").lower()
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if _CJK_RE.fullmatch(tok):
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i : i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok)
    return out


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict[str, Any]] = []
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _save_all(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _append(self, entry: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---------- 读写 ----------
    def add(self, kind: str, title: str, content: str,
            tags: list[str] | None = None, source: str = "agent") -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS}")
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        entry = {
            "id": uuid.uuid4().hex[:10],
            "kind": kind,
            "title": (title or "").strip()[:120],
            "content": (content or "").strip(),
            "tags": [str(t).strip() for t in (tags or []) if str(t).strip()][:12],
            "created": now,
            "updated": now,
            "source": source,
        }
        self._entries.append(entry)
        self._append(entry)
        return entry

    def remove(self, entry_id: str) -> bool:
        n = len(self._entries)
        self._entries = [e for e in self._entries if e.get("id") != entry_id]
        if len(self._entries) == n:
            return False
        self._save_all()
        return True

    def list(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        items = [e for e in self._entries if kind is None or e.get("kind") == kind]
        return items[-limit:]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        for e in self._entries:
            if e.get("id") == entry_id:
                return e
        return None

    # ---------- 检索 ----------
    def search(self, query: str, kind: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        q_tokens = set(_tokens(query))
        if not q_tokens:
            items = self.list(kind)
            return items[-limit:]
        scored = []
        for e in self._entries:
            if kind and e.get("kind") != kind:
                continue
            title_t = set(_tokens(e.get("title", "")))
            content_t = set(_tokens(e.get("content", "")))
            tag_t = set(_tokens(" ".join(e.get("tags", []))))
            score = (
                3.0 * len(q_tokens & title_t)
                + 1.0 * len(q_tokens & content_t)
                + 2.0 * len(q_tokens & tag_t)
            )
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], x[1].get("created", "")), reverse=False)
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:limit]]

    def digest(self, query: str, limit: int = 6) -> str:
        """给系统提示词的紧凑记忆摘要（自动挑选与当前话题相关的条目）。"""
        picked = self.search(query, limit=limit) if query else self.list(limit=limit)
        if not picked:
            # 无匹配时回退到最近的少量经验，保持"越用越懂业务"
            picked = [e for e in self._entries if e.get("kind") in ("lesson", "fact")][-limit:]
        if not picked:
            return ""
        lines = []
        for e in picked:
            label = KIND_LABELS.get(e.get("kind", ""), e.get("kind", ""))
            body = (e.get("content") or "").strip().replace("\n", " / ")
            if len(body) > 160:
                body = body[:160] + "…"
            lines.append(f"- [{label}] {e.get('title', '')}｜{body}")
        return "已沉淀的业务记忆（回答时优先采用与之吻合的事实）：\n" + "\n".join(lines)

    def stats(self) -> dict[str, int]:
        out = {k: 0 for k in KINDS}
        for e in self._entries:
            k = e.get("kind")
            if k in out:
                out[k] += 1
        out["total"] = len(self._entries)
        return out
