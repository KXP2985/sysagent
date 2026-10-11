"""构建 Windows 便携版：PyInstaller onedir + 启动.bat → dist/SysAgent-Portable/ 并打 zip。

用法（在项目根、.venv 激活或用 .venv 的 python）:
    python scripts/build_portable.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT_NAME = "SysAgent-Portable"


def main() -> int:
    if sys.platform != "win32":
        print("便携版打包请在 Windows 上执行（Linux 用源码/venv 方式运行）")
        return 1

    import PyInstaller  # noqa: F401  — 确保已 pip install pyinstaller

    hidden = [
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on", "uvicorn.lifespan.off",
        "websockets", "websockets.legacy", "yaml", "psutil",
        "agentd", "agentd.paths", "agentd.config", "agentd.server",
        "agentd.llm", "agentd.logmon", "agentd.memory", "agentd.capabilities",
        "agentd.models", "agentd.qqchannel", "agentd.reporting", "agentd.codescan",
        "agentd.tools", "agentd.agent", "agentd.system",
    ]
    datas = [
        (str(ROOT / "static"), "static"),
        (str(ROOT / "config.example.yaml"), "."),
        (str(ROOT / "samples"), "samples"),
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", "SysAgent",
        "--paths", str(ROOT),
        "--distpath", str(DIST / "build-dist"),
        "--workpath", str(DIST / "build-work"),
        "--specpath", str(DIST / "build-work"),
    ]
    for h in hidden:
        cmd += ["--hidden-import", h]
    for src, dst in datas:
        cmd += ["--add-data", f"{src};{dst}"]
    cmd.append(str(ROOT / "scripts" / "sysagent_entry.py"))

    print(">> PyInstaller:", " ".join(cmd[:8]), "...")
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    src_dir = DIST / "build-dist" / "SysAgent"
    port_dir = DIST / OUT_NAME
    if port_dir.exists():
        shutil.rmtree(port_dir)
    shutil.copytree(src_dir, port_dir)
    shutil.copy2(ROOT / "scripts" / "启动.bat", port_dir / "启动.bat")
    readme = ROOT / "docs" / "便携版说明.md"
    if readme.exists():
        shutil.copy2(readme, port_dir / "使用说明.md")

    zip_path = DIST / f"{OUT_NAME}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in port_dir.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(port_dir.parent))
    print(f"\n打包完成:\n  目录: {port_dir}\n  压缩包: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
