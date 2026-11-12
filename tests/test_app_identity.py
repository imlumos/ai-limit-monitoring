"""软件标识及可迁移配置回归:使用临时 data/、假托盘和临时桌面。"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from utils import app_settings, paths, proxy_config
from utils.proxy_config import ProxySettings, ProxyConfigError


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    from ui.float_window import FloatWindow
    from utils import account_store
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(paths, "get_resource_root", lambda: root)
    monkeypatch.setattr(paths, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(account_store.keyring, "get_password", lambda *args: None)
    monkeypatch.setattr("ui.float_window.autostart.is_enabled", lambda: False)
    monkeypatch.setattr(FloatWindow, "_init_screen_watchers", lambda self: None)
    monkeypatch.setattr(FloatWindow, "_setup_tray", lambda self: setattr(
        self, "tray_icon", SimpleNamespace(hide=Mock(), setIcon=Mock(), setToolTip=Mock())))
    old_proxy = proxy_config.get_proxy_settings()
    old_identity = (qapp.applicationName(), qapp.applicationDisplayName(), qapp.windowIcon())
    instance = FloatWindow()
    instance._apply_identity()
    try:
        yield instance
    finally:
        instance.poll_manager.stop()
        instance.close()
        proxy_config._config = old_proxy
        qapp.setApplicationName(old_identity[0])
        qapp.setApplicationDisplayName(old_identity[1])
        qapp.setWindowIcon(old_identity[2])


def image_file(path, color):
    image = QImage(64, 32, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    assert image.save(str(path))
    return path


@pytest.mark.parametrize("section", [
    {"name": ""}, {"name": " "}, {"name": "a" * 41}, {"name": "a\nb"},
    {"name": "../other"}, {"name": "name.exe."}, {"name": "CON"}, {"name": "nul.txt"},
    {"name": "COM1"}, {"name": "LPT¹"}, {"name": "x:y"}, {"name": True},
    {"icon": "../icon.ico"}, {"icon": "C:/icon.ico"}, {"icon": "app-icon.png"},
])
def test_identity_rejects_invalid_values(section):
    with pytest.raises(app_settings.SettingsError):
        app_settings.validate_settings({"application": section})


def test_identity_normalizes_name():
    assert app_settings.validate_settings({"application": {"name": " 我的用量看板 "}}) == {
        "application": {"name": "我的用量看板"}}


def test_name_icon_persist_and_same_filename_reloads(window, qapp, tmp_path):
    from ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window)
    dialog.app_name_edit.setText("我的用量看板")
    assert dialog._save_app_name()
    assert window.windowTitle() == qapp.applicationDisplayName() == "我的用量看板"
    window.tray_icon.setToolTip.assert_called_with("我的用量看板")
    for color in ("#ee2211", "#1166ee"):
        source = image_file(tmp_path / "source.png", color)
        assert dialog.set_app_icon(str(source))
        icon_path = Path(paths.get_data_path(app_settings.APP_ICON_FILE))
        assert icon_path.is_file()
        assert QImage(str(icon_path)).pixelColor(128, 128).name() == color
        assert window.windowIcon().pixmap(256, 256).toImage().pixelColor(128, 128).name() == color
    saved = app_settings.load_user_settings()["application"]
    assert saved == {"name": "我的用量看板", "icon": "app-icon.ico"}
    window.config = window._load_config()
    window._apply_identity()
    assert window.windowTitle() == "我的用量看板"
    dialog._on_reset_identity()
    assert window.windowTitle() == app_settings.DEFAULT_APP_NAME
    assert window.config["application"]["icon"] == ""
    assert not window.windowIcon().isNull()


def test_icon_save_failure_keeps_previous_icon(window, monkeypatch, tmp_path):
    from ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window)
    assert dialog.set_app_icon(str(image_file(tmp_path / "old.png", "red")))
    path = Path(paths.get_data_path(app_settings.APP_ICON_FILE))
    previous = path.read_bytes()
    monkeypatch.setattr(app_settings, "save_user_settings", Mock(
        side_effect=app_settings.SettingsError("保存失败")))
    assert not dialog.set_app_icon(str(image_file(tmp_path / "new.png", "blue")))
    assert path.read_bytes() == previous
    assert window.windowIcon().pixmap(256, 256).toImage().pixelColor(128, 128) == QColor("red")


def test_portable_config_restores_images_in_another_data_directory(window, monkeypatch, tmp_path):
    from ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window)
    window.apply_settings({"application": {"name": "测试看板"}})
    assert dialog.set_app_icon(str(image_file(tmp_path / "icon.png", "red")))
    assert dialog.set_background_image(str(image_file(tmp_path / "bg.png", "green")))
    bundle = tmp_path / "settings-export.toml"
    app_settings.export_settings(str(bundle), app_settings.current_overrides(window.config),
                                 ProxySettings("direct", ""))
    exported = bundle.read_text(encoding="utf-8")
    assert "[assets]" in exported and "api_key" not in exported.lower()
    destination = tmp_path / "new-install"
    monkeypatch.setattr(paths, "get_project_root", lambda: str(destination))
    assert dialog.import_from(str(bundle))
    assert Path(paths.get_data_path("app-icon.ico")).is_file()
    assert Path(paths.get_data_path("background.png")).is_file()
    assert app_settings.load_user_settings()["application"]["name"] == "测试看板"
    assert window._bg_pixmap.toImage().pixelColor(0, 0) == QColor("green")
    assert window.windowIcon().pixmap(256, 256).toImage().pixelColor(128, 128) == QColor("red")
    assert proxy_config.get_proxy_settings() == ProxySettings("direct", "")
    # 相同的图片文件名也必须刷新:导入覆盖前后的配置键值可能完全一样。
    assert dialog.set_background_image(str(image_file(tmp_path / "changed.png", "blue")))
    assert window._bg_pixmap.toImage().pixelColor(0, 0) == QColor("blue")
    assert dialog.import_from(str(bundle))
    assert window._bg_pixmap.toImage().pixelColor(0, 0) == QColor("green")


@pytest.mark.parametrize("assets", [
    {"app_icon": "***"},
    {"app_icon": base64.b64encode(b"not an image").decode()},
    {"../../outside": ""},
    {"background_image": ""},
])
def test_bad_asset_import_does_not_change_existing_settings(window, tmp_path, assets):
    from ui.settings_dialog import SettingsDialog
    path = tmp_path / "bad.toml"
    sections = {"application": {"name": "新名称", "icon": "app-icon.ico"}, "assets": assets}
    path.write_text(app_settings.dumps_toml(sections), encoding="utf-8")
    before = window.config.copy()
    assert not SettingsDialog(window).import_from(str(path))
    assert window.config == before
    assert not Path(paths.get_data_path("app-icon.ico")).exists()


def test_proxy_save_failure_keeps_icon_and_configuration(window, monkeypatch, tmp_path):
    from ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window)
    assert dialog.set_app_icon(str(image_file(tmp_path / "old.png", "red")))
    previous = Path(paths.get_data_path("app-icon.ico")).read_bytes()
    data = app_settings.read_app_icon(str(image_file(tmp_path / "new.png", "blue")))
    sections = {"application": {"name": "新名称", "icon": "app-icon.ico"},
                "proxy": {"mode": "direct", "url": ""},
                "assets": {"app_icon": base64.b64encode(data).decode()}}
    bundle = tmp_path / "import.toml"
    bundle.write_text(app_settings.dumps_toml(sections), encoding="utf-8")
    monkeypatch.setattr("ui.settings_dialog.save_and_apply_proxy", Mock(
        side_effect=ProxyConfigError("保存失败")))
    assert not dialog.import_from(str(bundle))
    assert Path(paths.get_data_path("app-icon.ico")).read_bytes() == previous
    assert window.windowTitle() == app_settings.DEFAULT_APP_NAME


def test_settings_save_failure_restores_proxy_and_images(window, monkeypatch, tmp_path):
    from ui.settings_dialog import SettingsDialog
    dialog = SettingsDialog(window)
    previous_proxy = proxy_config.get_proxy_settings()
    data = app_settings.read_app_icon(str(image_file(tmp_path / "new.png", "blue")))
    sections = {"application": {"name": "新名称", "icon": "app-icon.ico"},
                "proxy": {"mode": "direct", "url": ""},
                "assets": {"app_icon": base64.b64encode(data).decode()}}
    bundle = tmp_path / "import.toml"
    bundle.write_text(app_settings.dumps_toml(sections), encoding="utf-8")
    monkeypatch.setattr(app_settings, "save_user_settings", Mock(
        side_effect=app_settings.SettingsError("保存失败")))
    assert not dialog.import_from(str(bundle))
    assert proxy_config.get_proxy_settings() == previous_proxy
    assert not Path(paths.get_data_path("app-icon.ico")).exists()
    assert window.windowTitle() == app_settings.DEFAULT_APP_NAME


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Shell link integration")
@pytest.mark.parametrize("frozen", [False, True])
def test_shortcut_actual_com_properties_and_no_overwrite(window, monkeypatch, tmp_path, frozen):
    from utils import app_identity
    from PySide6.QtCore import QStandardPaths
    desktop = tmp_path / "temporary desktop"
    desktop.mkdir()
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(desktop))
    project_root = tmp_path / "app with spaces"
    project_root.mkdir()
    monkeypatch.setattr(paths, "get_project_root", lambda: str(project_root))
    monkeypatch.setattr(app_identity, "get_project_root", lambda: str(project_root))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    if frozen:
        monkeypatch.setattr(sys, "executable", str(project_root / "软件.exe"))
        app_settings.store_app_icon(str(image_file(tmp_path / "icon.png", "red")))
    identity = {"application": {"name": "我的看板 $()", "icon": "app-icon.ico" if frozen else ""}}
    shortcut = app_identity.create_desktop_shortcut(identity)
    assert Path(shortcut).name == "我的看板 $().lnk"
    env = os.environ.copy()
    env["AI_QUOTA_TEST_LINK"] = shortcut
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", """
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AI_QUOTA_TEST_LINK)
@{target=$link.TargetPath; arguments=$link.Arguments; icon=$link.IconLocation;
  working=$link.WorkingDirectory; description=$link.Description} | ConvertTo-Json -Compress
"""], env=env, capture_output=True, encoding="utf-8-sig", check=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    actual = json.loads(result.stdout)
    if frozen:
        assert actual["target"] == sys.executable
        assert actual["arguments"] == ""
    else:
        assert Path(actual["target"]).name == "pythonw.exe"
        assert actual["arguments"] == '"' + str(project_root / "main.py") + '"'
    assert actual["working"] == str(project_root)
    assert actual["icon"] == paths.get_data_path("app-icon.ico" if frozen else "default-app-icon.ico") + ",0"
    assert actual["description"] == "我的看板 $()"
    with pytest.raises(app_settings.SettingsError, match="已有同名"):
        app_identity.create_desktop_shortcut(identity)
