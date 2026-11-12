"""
窗口状态管理器 - 持久化窗口位置
================================

【这个文件做什么】
保存/读取悬浮窗在屏幕上的位置,实现"下次启动时窗口出现在上次拖到的位置"。

【整体方案(已与项目主程序对接)】
    启动 -> load_geometry() -> restoreGeometry() -> 屏幕外兜底检查
    用户拖动 -> mouseRelease -> save_geometry() 立即写盘
为什么不在程序退出时才保存?因为主程序用 os._exit(0) 强制退出,
不会触发 closeEvent/atexit/__del__,所以必须"拖完就写"才稳。

【关键概念:saveGeometry / restoreGeometry】
Qt 提供的一对 API:
    bytes = widget.saveGeometry()      把窗口位置/大小序列化为 QByteArray
    widget.restoreGeometry(bytes)      把字节流还原成窗口位置
优点:Qt 自己定义的格式,版本兼容性、屏幕信息都封装好了,
我们不用关心怎么解析 x、y、宽、高,直接当黑盒用。

【为什么用 hex 存进 JSON】
saveGeometry 返回的是 QByteArray(Qt 的字节容器,本质是 bytes)。
JSON 不能直接存 bytes,所以转成 hex 字符串(每字节 -> 两个十六进制字符),
读写对称、可读性也还行。

【路径计算】
路径推算委托给 utils.paths,不再自己写。
"""
import os
import json
from typing import Optional
from utils.paths import get_data_path, ensure_data_dir


class WindowManager:
    """窗口几何信息持久化"""

    STATE_FILE = "window_state.json"
    GEOMETRY_KEY = "geometry"     # JSON 里的字段名
    DOCK_EDGE_KEY = "dock_edge"   # 贴边状态('left'/'right'/'top'/'bottom'),没有则为 None

    @staticmethod
    def _get_state_file_path() -> str:
        """获取状态文件路径(委托给 utils.paths)"""
        return get_data_path(WindowManager.STATE_FILE)

    @staticmethod
    def save_geometry(geometry_bytes: bytes, dock_edge: str = None) -> bool:
        """
        保存窗口几何信息(可选:同时记录贴边状态)

        Args:
            geometry_bytes: QWidget.saveGeometry() 返回的 QByteArray
            dock_edge: 当前是否贴边,贴边时传 'left'/'right'/'top'/'bottom',
                       自由状态传 None(不写字段,兼容旧版文件)

        Returns:
            是否保存成功

        【调用链】
            FloatWindow.mouseReleaseEvent()
              -> self.saveGeometry()              # Qt API,返回 QByteArray
              -> WindowManager.save_geometry(...) # 这里:转 hex 写入 JSON
        """
        try:
            ensure_data_dir()

            # bytes(geometry_bytes):QByteArray 转 Python bytes
            # .hex():bytes 的方法,把每字节变成两个十六进制字符
            # 例:b'\\x01\\x02' -> "0102"
            geometry_hex = bytes(geometry_bytes).hex()

            # 贴边状态(自由状态不写字段,避免污染旧记录)
            payload = {WindowManager.GEOMETRY_KEY: geometry_hex}
            if dock_edge:
                payload[WindowManager.DOCK_EDGE_KEY] = dock_edge

            state_file = WindowManager._get_state_file_path()
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f)

            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("保存窗口位置失败: %s", e, exc_info=True)
            return False

    @staticmethod
    def load_dock_edge() -> Optional[str]:
        """读取上次退出时是否处于贴边状态(没有记录/文件损坏返回 None)"""
        try:
            state_file = WindowManager._get_state_file_path()
            if not os.path.exists(state_file):
                return None
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get(WindowManager.DOCK_EDGE_KEY)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("加载贴边状态失败: %s", e, exc_info=True)
            return None

    @staticmethod
    def load_geometry() -> Optional[bytes]:
        """
        加载窗口几何信息

        Returns:
            bytes(可直接给 QByteArray(bytes) 构造)或 None

        【对称设计】
            保存用 .hex(),读取就用 bytes.fromhex(),正好互为逆运算。
        """
        try:
            state_file = WindowManager._get_state_file_path()
            if not os.path.exists(state_file):
                return None

            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            geometry_hex = data.get(WindowManager.GEOMETRY_KEY)
            if not geometry_hex:
                return None

            # bytes.fromhex(hex_string):hex 字符串还原为 bytes
            return bytes.fromhex(geometry_hex)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("加载窗口位置失败: %s", e, exc_info=True)
            return None
