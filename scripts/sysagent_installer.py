"""SysAgent 正式安装器（也可用 --uninstall 卸载）。

安装策略：
- 当前用户安装（默认，免管理员）：装到 %LOCALAPPDATA%\\Programs\\SysAgent，数据在程序目录
- 所有用户安装（需管理员）：装到 %ProgramFiles%\\SysAgent，数据在 %APPDATA%\\SysAgent
  （写入 installed.flag，由 agentd.paths 识别）
- 建开始菜单/桌面快捷方式、写入「应用和功能」卸载项、可选开机自启
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

APP = "SysAgent"
APP_FULL = "SysAgent 智能运维体"
PUBLISHER = "SysAgent Project"


def utf8_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        os.system("chcp 65001 >nul")
    except Exception:
        pass


def payload_zip() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "payload" / "payload.zip"
    return Path(__file__).resolve().parent.parent / "dist" / "build-work" / "payload.zip"


def run_ps(script: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=False, capture_output=True)


def make_shortcuts(app_dir: Path, all_users: bool) -> None:
    # 全局位置失败（无管理员权限）时自动降级到当前用户
    try:
        if all_users:
            start_menu = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                          / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP)
            start_menu.mkdir(parents=True, exist_ok=True)
            desktop = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
        else:
            raise PermissionError  # 走用户分支
    except PermissionError:
        if all_users:
            print("   (无管理员权限，快捷方式改为放到当前用户)")
        all_users = False
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        start_menu = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP
        start_menu.mkdir(parents=True, exist_ok=True)
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"

    bat = app_dir / "启动.bat"
    exe = app_dir / "SysAgent.exe"
    url = start_menu / "控制台.lnk"
    # 快捷方式生成（WScript.Shell）
    ps = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("{start_menu / '启动 SysAgent.lnk'}")
$s.TargetPath = "{bat}"
$s.WorkingDirectory = "{app_dir}"
$s.Description = "启动 SysAgent 服务并打开控制台"
$s.Save()
$s2 = $ws.CreateShortcut("{start_menu / '卸载 SysAgent.lnk'}")
$s2.TargetPath = "{app_dir / 'SysAgent-Installer.exe'}"
$s2.Arguments = "--uninstall"
$s2.WorkingDirectory = "{app_dir}"
$s2.Save()
$s3 = $ws.CreateShortcut("{desktop / 'SysAgent.lnk'}")
$s3.TargetPath = "{bat}"
$s3.WorkingDirectory = "{app_dir}"
$s3.Save()
'''
    run_ps(ps)
    # 控制台 URL 快捷方式
    try:
        url_txt = start_menu / "SysAgent 控制台.url"
        url_txt.write_text("[InternetShortcut]\nURL=http://127.0.0.1:8765/\n", encoding="utf-8")
    except OSError:
        pass
    # 无用变量防告警
    _ = (exe, url)


def write_uninstall_entry(app_dir: Path, all_users: bool) -> None:
    unins = f'"{app_dir / "SysAgent-Installer.exe"}" --uninstall'
    root = "HKLM" if all_users else "HKCU"
    key = rf"{root}\Software\Microsoft\Windows\CurrentVersion\Uninstall\SysAgent"
    cmds = [
        f'reg add "{key}" /v DisplayName /d "{APP_FULL}" /f',
        f'reg add "{key}" /v DisplayIcon /d "{app_dir / "SysAgent.exe"}" /f',
        f'reg add "{key}" /v UninstallString /d "{unins}" /f',
        f'reg add "{key}" /v Publisher /d "{PUBLISHER}" /f',
        f'reg add "{key}" /v NoModify /t REG_DWORD /d 1 /f',
        f'reg add "{key}" /v NoRepair /t REG_DWORD /d 1 /f',
    ]
    for c in cmds:
        r = subprocess.run(c, shell=True, capture_output=True)
        if r.returncode != 0 and all_users:
            # 无权限写 HKLM 时降级 HKCU
            write_uninstall_entry(app_dir, all_users=False)
            return


def set_autostart(app_dir: Path, enable: bool) -> None:
    key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    if enable:
        subprocess.run(
            f'reg add "{key}" /v SysAgent /d "{app_dir / "SysAgent.exe"}" /f',
            shell=True, capture_output=True)
    else:
        subprocess.run(f'reg delete "{key}" /v SysAgent /f', shell=True, capture_output=True)


def install(all_users: bool, dest: Path, autostart: bool) -> None:
    zpath = payload_zip()
    if not zpath.exists():
        raise SystemExit(f"[错误] 找不到安装载荷: {zpath}")
    dest.mkdir(parents=True, exist_ok=True)
    print(f">> 安装到: {dest}")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    # 复制安装器自身到应用目录，作为卸载入口
    if getattr(sys, "frozen", False):
        try:
            shutil.copy2(sys.executable, dest / "SysAgent-Installer.exe")
        except OSError as e:
            print(f"   (复制卸载器失败: {e})")
    if all_users:
        (dest / "installed.flag").write_text("installed\n", encoding="utf-8")
    make_shortcuts(dest, all_users)
    write_uninstall_entry(dest, all_users)
    if autostart:
        set_autostart(dest, True)
    print(f">> 安装完成。双击桌面「SysAgent」即可启动，控制台 http://127.0.0.1:8765/")


def uninstall(app_dir: Path | None) -> None:
    print(">> 开始卸载 SysAgent …")
    subprocess.run('taskkill /F /IM SysAgent.exe /T', shell=True, capture_output=True)
    time.sleep(0.5)

    # 快捷方式
    for base in (Path(os.environ.get("APPDATA", "")), Path(os.environ.get("PROGRAMDATA", ""))):
        if base:
            shutil.rmtree(base / "Microsoft/Windows/Start Menu/Programs/SysAgent", ignore_errors=True)
    for desk in (Path(os.environ.get("USERPROFILE", "")) / "Desktop",
                 Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"):
        if desk:
            for n in ("SysAgent.lnk",):
                try:
                    (desk / n).unlink(missing_ok=True)
                except OSError:
                    pass

    set_autostart(app_dir or Path("."), False)
    for root in ("HKCU", "HKLM"):
        subprocess.run(
            rf'reg delete "{root}\Software\Microsoft\Windows\CurrentVersion\Uninstall\SysAgent" /f',
            shell=True, capture_output=True)

    # 程序目录
    if app_dir and app_dir.exists():
        ans = input(f"是否删除程序目录 {app_dir}？[y/N] ").strip().lower()
        if ans == "y":
            try:
                shutil.rmtree(app_dir, ignore_errors=True)
            except OSError:
                pass
            if app_dir.exists():
                # 卸载器正在本目录内运行，延迟删除
                subprocess.Popen(
                    f'cmd /c ping -n 4 127.0.0.1 >nul & rd /s /q "{app_dir}"',
                    shell=True)
                print(f"   已安排删除 {app_dir}（卸载器退出后自动清理）")
            else:
                print(f"   已删除 {app_dir}")

    # 数据目录（安装模式在 APPDATA）
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "SysAgent"
    if appdata.exists():
        ans = input(f"是否删除数据（记忆/配置/模型）{appdata}？[y/N] ").strip().lower()
        if ans == "y":
            shutil.rmtree(appdata, ignore_errors=True)
            print("   已删除数据目录")
        else:
            print("   已保留数据目录")

    print(">> 卸载完成。")
    input("按回车退出…")


def default_dest(all_users: bool) -> Path:
    if all_users:
        return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / APP
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Programs" / APP


def wizard() -> None:
    print("=" * 52)
    print(f"  {APP_FULL} 安装程序")
    print("=" * 52)
    print("[1] 仅当前用户安装（推荐，免管理员）")
    print("[2] 所有用户安装（Program Files，需管理员权限）")
    print("[0] 取消")
    choice = input("请选择 [1]: ").strip() or "1"
    if choice == "0":
        return
    all_users = choice == "2"

    default = default_dest(all_users)
    raw = input(f"安装目录 [{default}]: ").strip()
    dest = Path(raw).expanduser() if raw else default

    autostart = input("是否开机自动启动服务？[y/N]: ").strip().lower() == "y"

    try:
        install(all_users, dest, autostart)
    except PermissionError:
        print("[错误] 没有写入权限。请以管理员身份运行，或改用「仅当前用户安装」。")
        input("按回车退出…")
        return

    if input("现在启动 SysAgent？[Y/n]: ").strip().lower() != "n":
        subprocess.Popen(
            ["cmd", "/c", "start", "SysAgent", str(dest / "启动.bat")],
            cwd=str(dest), shell=False)


def main() -> None:
    utf8_console()
    parser = argparse.ArgumentParser(description="SysAgent 安装器")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    parser.add_argument("--all-users", action="store_true", help="所有用户安装（配合 --silent）")
    parser.add_argument("--dir", default=None, help="指定安装目录（配合 --silent）")
    parser.add_argument("--autostart", action="store_true", help="开机自启（配合 --silent）")
    parser.add_argument("--silent", action="store_true", help="静默安装（不询问）")
    args = parser.parse_args()

    if args.uninstall:
        app_dir = Path(__file__).resolve().parent if not getattr(sys, "frozen", False) else Path(sys.executable).resolve().parent
        uninstall(app_dir)
        return

    if args.silent:
        dest = Path(args.dir) if args.dir else default_dest(args.all_users)
        install(args.all_users, dest, args.autostart)
        return

    wizard()


if __name__ == "__main__":
    main()
