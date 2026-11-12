"""
拖拽改大小的边缘手柄 (ResizeGrip)
================================

无边框窗口没有系统边框可拖,这里在窗口边缘贴几条透明的细条控件:
鼠标悬停显示改大小光标,按住拖动把位移交给主窗口处理。
自己处理鼠标事件(不用 startSystemResize),这样尺寸始终由主窗口的
_relayout 统一计算,不会和"高度随内容/贴边细条"等逻辑打架。

主窗口需提供:
    _begin_user_resize()
    _user_resize(edge: str, dx: int, dy: int)   # dx/dy 为相对按下时的累计位移
    _end_user_resize()
"""
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QWidget

GRIP_SIZE = 6            # 边条粗细(像素)
CORNER_SIZE = 14         # 下方两角的方块边长

_CURSORS = {
    "left": Qt.CursorShape.SizeHorCursor,
    "right": Qt.CursorShape.SizeHorCursor,
    "bottom": Qt.CursorShape.SizeVerCursor,
    "bottomleft": Qt.CursorShape.SizeBDiagCursor,
    "bottomright": Qt.CursorShape.SizeFDiagCursor,
}
EDGES = tuple(_CURSORS)


class ResizeGrip(QWidget):
    def __init__(self, window, edge: str):
        super().__init__(window)
        self._window = window
        self.edge = edge
        self._press_global = None
        self.setCursor(_CURSORS[edge])

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._window._begin_user_resize()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press_global is not None:
            d: QPoint = event.globalPosition().toPoint() - self._press_global
            self._window._user_resize(self.edge, d.x(), d.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._press_global is not None and event.button() == Qt.MouseButton.LeftButton:
            self._press_global = None
            self._window._end_user_resize()
        event.accept()


def grip_rects(w: int, h: int, bottom_enabled: bool) -> dict:
    """各手柄在窗口内的矩形 (x, y, w, h);bottom_enabled=False 时只给左右两条。"""
    g, c = GRIP_SIZE, CORNER_SIZE
    side_h = h - c if bottom_enabled else h
    rects = {"left": (0, 0, g, side_h), "right": (w - g, 0, g, side_h)}
    if bottom_enabled:
        rects.update({
            "bottom": (c, h - g, w - 2 * c, g),
            "bottomleft": (0, h - c, c, c),
            "bottomright": (w - c, h - c, c, c),
        })
    return rects
