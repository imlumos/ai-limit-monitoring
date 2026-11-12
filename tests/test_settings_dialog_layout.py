"""高级设置窗口的屏幕边界和滚动回归,不读写账号或设置。"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.settings_dialog import SettingsDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("available", [
    QRect(0, 0, 800, 800),
    QRect(0, 40, 640, 440),  # 顶部任务栏和较矮的屏幕
    QRect(-800, -600, 800, 600),  # 位于主屏左上方的副屏
])
@pytest.mark.parametrize("position", ["top", "bottom", "left", "right"])
def test_settings_frame_stays_on_screen(qapp, monkeypatch, available, position):
    parent = QWidget()
    parent.config = {}
    parent.resize(300, 40)
    points = {
        "top": available.topLeft(),
        "bottom": available.bottomLeft(),
        "left": available.topLeft(),
        "right": available.topRight(),
    }
    parent.move(points[position])
    dialog = SettingsDialog(parent)
    monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(
        availableGeometry=lambda: available))
    # 模拟 Qt 相对靠边父窗口居中后,标题栏或侧边超出屏幕。
    dialog.move(parent.x() - dialog.width() // 2,
                parent.y() - dialog.height() // 2)
    try:
        dialog.show()
        qapp.processEvents()
        assert available.contains(dialog.frameGeometry())
        assert dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible()
    finally:
        dialog.close()
        parent.close()


def test_small_screen_keeps_last_setting_reachable(qapp, monkeypatch):
    parent = QWidget()
    parent.config = {}
    dialog = SettingsDialog(parent)
    available = QRect(0, 0, 640, 400)
    monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(
        availableGeometry=lambda: available))
    try:
        dialog.show()
        qapp.processEvents()
        assert available.contains(dialog.frameGeometry())
        assert dialog.scroll_area.verticalScrollBar().maximum() > 0
        dialog.scroll_area.ensureWidgetVisible(dialog.export_btn)
        qapp.processEvents()
        viewport = dialog.scroll_area.viewport()
        button_rect = QRect(dialog.export_btn.mapTo(viewport, dialog.export_btn.rect().topLeft()),
                            dialog.export_btn.size())
        assert viewport.rect().contains(button_rect)
    finally:
        dialog.close()
        parent.close()
