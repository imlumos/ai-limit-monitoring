"""按自定义名称和图标创建 Windows 桌面快捷方式,不修改 EXE 本身。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from PySide6.QtCore import QStandardPaths

from utils import app_settings
from utils.paths import get_data_path, get_project_root, get_resource_path


def create_desktop_shortcut(config: dict) -> str:
    if sys.platform != "win32":
        raise app_settings.SettingsError("创建桌面快捷方式仅支持 Windows")
    identity = app_settings.validate_settings({"application": config.get("application", {})})["application"]
    name = identity.get("name", app_settings.DEFAULT_APP_NAME)
    desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    if not desktop or not Path(desktop).is_dir():
        raise app_settings.SettingsError("未找到桌面目录")
    shortcut = Path(desktop) / (name + ".lnk")
    # 不覆盖用户已有的快捷方式;更名后也不自动删除旧快捷方式。
    if shortcut.exists():
        raise app_settings.SettingsError("桌面已有同名快捷方式,请先重命名或移走后再创建")
    icon = app_settings.app_icon_path(identity.get("icon", ""))
    if not icon or not Path(icon).is_file():
        icon = get_data_path("default-app-icon.ico")
        try:
            Path(icon).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(get_resource_path("favicon.ico"), icon)
        except OSError:
            raise app_settings.SettingsError("无法保存默认图标,请检查 data 目录权限") from None
    target = sys.executable
    arguments = ""
    if not getattr(sys, "frozen", False):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.is_file():
            target = str(pythonw)
        arguments = subprocess.list2cmdline([str(Path(get_project_root()) / "main.py")])
    # 用户名称、路径只经环境变量传给 COM 属性,不拼入 PowerShell 代码。
    env = os.environ.copy()
    env.update({"AI_QUOTA_SHORTCUT": str(shortcut), "AI_QUOTA_TARGET": target,
                "AI_QUOTA_ARGUMENTS": arguments, "AI_QUOTA_WORKDIR": get_project_root(),
                "AI_QUOTA_ICON": icon, "AI_QUOTA_NAME": name})
    script = """
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $env:AI_QUOTA_SHORTCUT) { throw 'Shortcut already exists' }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:AI_QUOTA_SHORTCUT)
$link.TargetPath = $env:AI_QUOTA_TARGET
$link.Arguments = $env:AI_QUOTA_ARGUMENTS
$link.WorkingDirectory = $env:AI_QUOTA_WORKDIR
$link.IconLocation = $env:AI_QUOTA_ICON + ',0'
$link.Description = $env:AI_QUOTA_NAME
$link.Save()
"""
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise app_settings.SettingsError("创建桌面快捷方式失败,请检查桌面目录权限") from None
    if not shortcut.is_file():
        raise app_settings.SettingsError("快捷方式未创建成功")
    return str(shortcut)
