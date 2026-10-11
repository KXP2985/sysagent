"""构建正式安装器 SysAgent-Installer.exe（自研向导，无外部编译器依赖）。

用法: python scripts/build_installer.py
产物: dist/SysAgent-Installer.exe
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def ensure_payload() -> Path:
    portable = DIST / "SysAgent-Portable"
    if not (portable / "SysAgent.exe").exists():
        print(">> 未发现便携产物，先构建便携版…")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_portable.py")],
                       check=True, cwd=str(ROOT))
    work = DIST / "build-work"
    work.mkdir(parents=True, exist_ok=True)
    zpath = work / "payload.zip"
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in portable.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(portable))
    print(f">> 载荷: {zpath} ({zpath.stat().st_size / 1024 / 1024:.1f} MB)")
    return zpath


def main() -> int:
    if sys.platform != "win32":
        print("安装器请在 Windows 上构建")
        return 1
    import PyInstaller  # noqa: F401

    ensure_payload()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "SysAgent-Installer",
        "--distpath", str(DIST / "build-dist"),
        "--workpath", str(DIST / "build-work" / "installer-work"),
        "--specpath", str(DIST / "build-work" / "installer-work"),
        "--add-data", f"{DIST / 'build-work' / 'payload.zip'};payload",
        str(ROOT / "scripts" / "sysagent_installer.py"),
    ]
    print(">> PyInstaller (onefile installer)…")
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    out = DIST / "SysAgent-Installer.exe"
    shutil.copy2(DIST / "build-dist" / "SysAgent-Installer.exe", out)
    print(f"\n安装器完成: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    print("静默安装示例: SysAgent-Installer.exe --silent --dir D:\\SysAgent --autostart")
    print("静默卸载示例: SysAgent-Installer.exe --uninstall --silent  (卸载为交互式，会询问数据)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
