"""受限代码学习：扫描项目、提炼风格画像、在沙箱内生成脚手架。

边界（刻意保守）：
- 只能读 code.scan_dirs 之内的项目
- 脚手架只能写 code.workspace_dir/<name>/ 之内，路径含 .. 一律拒绝
- 写入需工具层审批；本模块不做代码执行
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROJECT_MARKERS = (
    ".git", "pyproject.toml", "setup.py", "package.json", "go.mod",
    "Cargo.toml", "pom.xml", "build.gradle", "composer.json", "requirements.txt",
)
STYLE_FILES = (
    "README.md", "README.rst", "README.txt", "README",
    "pyproject.toml", "setup.py", "setup.cfg", "package.json", "tsconfig.json",
    "go.mod", "Cargo.toml", "requirements.txt", "Pipfile", "Makefile",
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", "ruff.toml", "tox.ini",
)
SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".cs", ".rb", ".php"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".idea", ".vscode"}
_MAX_FILES = 8
_MAX_BYTES = 6000  # 单文件读取上限


def _under(path: Path, roots: list[Path]) -> bool:
    for r in roots:
        try:
            path.resolve().relative_to(r.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def list_projects(scan_dirs: list[str], max_depth: int = 3) -> list[dict[str, Any]]:
    roots = [Path(d).expanduser().resolve() for d in scan_dirs]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            p = Path(dirpath)
            if len(p.parts) - root_depth > max_depth:
                dirnames.clear()
                continue
            markers = [m for m in PROJECT_MARKERS if (p / m).exists()]
            if markers:
                key = str(p.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "path": key,
                        "name": p.name,
                        "markers": markers[:5],
                        "languages": _guess_lang(filenames),
                    })
            if found and p != root:
                # 项目根目录不再向里扫，避免把子包当项目
                dirnames[:] = [d for d in dirnames if not (p / d / "pyproject.toml").exists()
                               and not (p / d / "package.json").exists()]
    return found[:50]


def _guess_lang(filenames: list[str]) -> list[str]:
    ext_map = {".py": "python", ".js": "javascript", ".ts": "typescript", ".go": "go",
               ".rs": "rust", ".java": "java", ".rb": "ruby", ".php": "php", ".cs": "csharp"}
    langs = set()
    for fn in filenames:
        ext = Path(fn).suffix.lower()
        if ext in ext_map:
            langs.add(ext_map[ext])
    return sorted(langs)[:5]


def analyze_project(project_path: str, scan_dirs: list[str]) -> dict[str, Any]:
    roots = [Path(d).expanduser().resolve() for d in scan_dirs]
    p = Path(project_path).expanduser().resolve()
    if not _under(p, roots) or not p.is_dir():
        return {"error": f"路径不在允许的扫描目录内或不是目录: {project_path}"}

    tree: list[str] = []
    style_files: dict[str, str] = {}
    sources: dict[str, str] = {}

    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).resolve().relative_to(p)
        depth = len(rel.parts) if str(rel) != "." else 0
        if depth > 2:
            dirnames.clear()
            continue
        indent = "  " * depth
        for fn in sorted(filenames)[:30]:
            tree.append(f"{indent}{fn}")
        if depth <= 1:
            for fn in filenames:
                if fn in STYLE_FILES and len(style_files) < 6:
                    style_files[fn] = _read_head(p / fn)
                elif Path(fn).suffix.lower() in SOURCE_EXTS and len(sources) < _MAX_FILES:
                    sources[fn] = _read_head(p / fn, 2500)

    # src/ 优先读源码
    for sub in ("src", "lib", "app", "cmd", "internal"):
        sp = p / sub
        if sp.is_dir():
            for fn in sorted(sp.rglob("*")):
                if fn.is_file() and fn.suffix.lower() in SOURCE_EXTS and len(sources) < _MAX_FILES:
                    key = str(fn.relative_to(p))
                    if key not in sources:
                        sources[key] = _read_head(fn, 2500)

    fingerprint = {
        "project": p.name,
        "path": str(p),
        "languages": _guess_lang(os.listdir(p)),
        "markers": [m for m in PROJECT_MARKERS if (p / m).exists()],
        "file_count": sum(len(files) for _, _, files in os.walk(p)),
        "tree": "\n".join(tree[:80]),
        "style_files": style_files,
        "sample_sources": sources,
    }
    return fingerprint


def _read_head(path: Path, limit: int = _MAX_BYTES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(limit)
        return text + ("\n…(截断)" if path.stat().st_size > limit else "")
    except OSError as e:
        return f"(读取失败: {e})"


def scaffold_files(workspace_dir: str, name: str,
                   files: list[dict[str, str]]) -> dict[str, Any]:
    """在 workspace_dir/name/ 下写脚手架文件。返回结果或错误（调用方负责审批）。"""
    if not re_match_name(name):
        return {"error": "项目名只允许字母数字、下划线、连字符，且不能以点开头"}
    ws = Path(workspace_dir).expanduser().resolve()
    root = (ws / name).resolve()
    if root.exists() and any(root.iterdir()):
        return {"error": f"目标目录已存在且非空: {root}"}
    if not isinstance(files, list) or not files:
        return {"error": "files 不能为空"}
    if len(files) > 40:
        return {"error": "单次最多 40 个文件"}

    written = []
    total = 0
    try:
        root.mkdir(parents=True, exist_ok=True)
        for item in files:
            rel = str(item.get("path") or "").strip().replace("\\", "/")
            content = str(item.get("content") or "")
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                return {"error": f"非法路径: {rel}"}
            total += len(content)
            if total > 500_000:
                return {"error": "内容总大小超过 500KB"}
            dest = (root / rel).resolve()
            if not _under(dest, [root]):
                return {"error": f"路径越界: {rel}"}
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            written.append(rel)
    except OSError as e:
        return {"error": f"写入失败: {e}"}
    return {"ok": True, "path": str(root), "written": written}


def re_match_name(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name or ""))
