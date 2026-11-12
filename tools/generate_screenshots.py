"""
AI 额度悬浮窗 - 页面效果图生成脚本
====================================

【这个文件做什么】
实例化真实的 FloatWindow / AddAccountDialog,注入模拟账号与额度数据,
用 QWidget.grab() 把每个页面/状态渲染成 PNG。
截图由"真实 UI 代码"绘制,不是手绘示意图 —— UI 改版后重跑一次即可全部更新。

【为什么不用 offscreen 平台】offscreen 的字体库没有中文回退,会渲染出
一排"豆腐块"。改用原生 windows 平台 + WA_DontShowOnScreen:窗口在 Qt
内部完成布局与绘制(字体、DPI 都是真的),但不会真正映射到屏幕上,
用户看不到任何闪现。

【清晰度】设 QT_SCALE_FACTOR=2:所有内容按 2 倍物理像素渲染(逻辑尺寸
不变),文字是真正的高清 2x,而不是事后放大。画布为白色,衬托深色窗体。

【用法】(项目根目录下)
    .venv/Scripts/python.exe tools/generate_screenshots.py
输出:image/screenshots/*.png

【覆盖的状态】
    01 收起悬浮条-单源        02 收起悬浮条-多源
    03 展开-账号列表页        04 展开-账号列表页(Key 失效)
    05 展开-设置页            06 贴边收起-左侧竖条
    07 贴边收起-顶部横条      08 添加账号弹窗
    09 无账号-悬浮条空态      10 无账号-账号管理引导页
    11 高级设置(含配置导入导出)

【数据安全】
生成前把所有落盘/读盘操作替换成 no-op:
    - AccountStore._load            -> 不读、不迁移真实 accounts.json
    - save_ui_state                 -> 不写 ui_state.json
    - WindowManager.save_geometry   -> 不写窗口状态
    - WindowManager.load_geometry   -> 返回 None(窗口居中,行为确定)
并且不调用 startup_flow / poll_manager.start,全程无网络请求、无真实数据读写。
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)   # 配置/资源文件按项目根目录的相对路径读取

# 2 倍缩放渲染:逻辑尺寸不变,物理像素翻倍 —— 文字是真实 2x 高清,非事后放大。
# 必须在 QApplication 创建之前设置。
os.environ.setdefault("QT_SCALE_FACTOR", "2")

# ---------- 1) 数据安全补丁(在导入业务模块之前打好) ----------
from utils import account_store as _account_store_mod
from utils import ui_state as _ui_state_mod
from utils import window_manager as _wm
from utils import paths as _paths

# 资源仍读源码目录,用户数据全部重定向到临时目录。
_temporary_data = tempfile.TemporaryDirectory(prefix="ai-quota-screenshots-")
_paths.get_project_root = lambda: _temporary_data.name
_paths.get_resource_root = lambda: str(ROOT)


def _fake_load(self):
    """替代 AccountStore._load:内存里给空列表,不碰真实 accounts.json。"""
    self._accounts = []
    self._migration_done = True


def _noop(*_args, **_kwargs):
    return None


def _deny_access(*_args, **_kwargs):
    raise RuntimeError("截图生成不允许访问网络或真实凭据")


_account_store_mod.AccountStore._load = _fake_load
_ui_state_mod.save_ui_state = _noop
_wm.WindowManager.save_geometry = staticmethod(_noop)
_wm.WindowManager.load_geometry = staticmethod(lambda: None)

import requests
requests.Session.request = _deny_access
for _method in ("get_password", "set_password", "delete_password"):
    setattr(_account_store_mod.keyring, _method, _deny_access)

# PySide6 导入统一放在补丁之后
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from api.models import (KIND_MONEY, KIND_PERCENT, QuotaSnapshot, QuotaTier,
                        TIER_FIVE_HOUR, TIER_SEVEN_DAY, TIER_WALLET,
                        TIER_WEEKLY_LIMIT)
from ui import float_window as _fw
from ui.detail_panel import AddAccountDialog
from ui.float_window import FloatWindow
from ui.settings_dialog import SettingsDialog
from api.provider_base import QuotaProvider
from api.providers import cursor as _cursor
from utils.account_store import Account
from utils.ui_state import MODE_MULTI, MODE_SINGLE

# float_window 里 save_ui_state 是"按名导入",补丁要单独再打一处
_fw.save_ui_state = _noop
_fw.autostart.is_enabled = lambda: False
FloatWindow._setup_tray = lambda self: setattr(self, "tray_icon", SimpleNamespace(
    hide=_noop, setIcon=_noop, setToolTip=_noop))
FloatWindow._init_screen_watchers = _noop
QuotaProvider.read_cli_json = staticmethod(_deny_access)
_cursor._read_local_token = _deny_access

OUT_DIR = ROOT / "image" / "screenshots"

# ---------- 2) 模拟数据 ----------
# 所有账号名称、额度、时间均为示例,不读取实拍截图或本机账号。


def _account(aid: str, label: str, provider_id: str) -> Account:
    return Account(id=aid, label=label, provider_id=provider_id,
                   enabled=True, created_at=0)


VOLC, DS, CLAUDE = "acc-volc", "acc-ds", "acc-claude"
CODEX, CURSOR = "acc-codex", "acc-cursor"
ACCOUNTS = [
    _account(CLAUDE, "Claude", "claude"),
    _account(CODEX, "GPT", "codex"),
    _account(CURSOR, "Cursor", "cursor"),
    _account(VOLC, "火山", "volcengine"),
    _account(DS, "DeepSeek", "deepseek"),
]


def _snapshots(key_expired: bool = False) -> dict:
    """模拟五个账号;key_expired=True 时把 Codex 换成 401 失效态。"""
    snaps = {
        VOLC: QuotaSnapshot(
            account_id=VOLC, provider_id="volcengine", success=True,
            credential_message="Coding Plan",
            tiers=[
                QuotaTier(name=TIER_FIVE_HOUR, kind=KIND_PERCENT,
                          utilization=63.5, resets_at="今天 14:32"),
                QuotaTier(name=TIER_WEEKLY_LIMIT, kind=KIND_PERCENT,
                          utilization=41.2, resets_at="周一 00:00"),
            ]),
        CODEX: QuotaSnapshot(
            account_id=CODEX, provider_id="codex", success=True,
            credential_message="ChatGPT 订阅",
            tiers=[
                QuotaTier(name=TIER_FIVE_HOUR, kind=KIND_PERCENT,
                          utilization=18.0, resets_at="今天 16:05"),
                QuotaTier(name=TIER_WEEKLY_LIMIT, kind=KIND_PERCENT,
                          utilization=9.5, resets_at="周一 00:00"),
            ]),
        DS: QuotaSnapshot(
            account_id=DS, provider_id="deepseek", success=True,
            tiers=[
                QuotaTier(name=TIER_WALLET, kind=KIND_MONEY,
                          remaining=87.30, unit="CNY"),
            ]),
        CURSOR: QuotaSnapshot(
            account_id=CURSOR, provider_id="cursor", success=True,
            credential_message="Cursor Pro+",
            tiers=[
                QuotaTier(name="monthly", kind=KIND_PERCENT, utilization=68,
                          resets_at="12-01 00:00"),
                QuotaTier(name="cursor_auto", kind=KIND_PERCENT, utilization=52,
                          resets_at="12-01 00:00", display_label="每月·Auto", display_short_label="Auto"),
                QuotaTier(name="cursor_api", kind=KIND_PERCENT, utilization=84,
                          resets_at="12-01 00:00", display_label="每月·API", display_short_label="API"),
                QuotaTier(name="cursor_grok_bot", kind=KIND_PERCENT, utilization=12,
                          resets_at="11-19 09:00", display_label="Grok Bot·每周", display_short_label="Grok"),
            ]),
        CLAUDE: QuotaSnapshot(
            account_id=CLAUDE, provider_id="claude", success=True,
            credential_message="Pro 订阅",
            tiers=[
                QuotaTier(name=TIER_FIVE_HOUR, kind=KIND_PERCENT,
                          utilization=42, resets_at="今天 17:00"),
                QuotaTier(name=TIER_SEVEN_DAY, kind=KIND_PERCENT,
                          utilization=76.0, resets_at="周四 12:00"),
            ]),
    }
    if key_expired:
        snaps[CODEX] = QuotaSnapshot.failure_snapshot(
            "codex", credential_status="expired",
            error_message="鉴权失败:Key 无效或已失效 (401)")
    for aid, snapshot in snaps.items():
        snapshot.account_id = aid
        snapshot.queried_at = 1794443400000  # 固定示例时间
    return snaps


# ---------- 3) 状态切换与截图 ----------


def apply_state(win: FloatWindow, mode: str, accounts, snapshots) -> None:
    """把窗口重置为"自由态 + 收起"并注入指定数据(不落盘)。"""
    win._docked = False
    win._dock_edge = None
    win._is_expanded = False
    win._is_animating = False
    win._height_animation.stop()
    win._dock_geometry_animation.stop()
    win.detail_panel.setVisible(False)
    win.setMinimumHeight(win.config['window']['height'])
    win.setMaximumHeight(1000)
    win.account_store._accounts = list(accounts)
    win._snapshots = dict(snapshots)
    win.detail_panel.set_snapshots(win._snapshots)
    win.ui_state = {
        'display_mode': mode,
        'active_account_id': accounts[0].id if accounts else None,
        'visible_account_ids': [a.id for a in accounts],
        'window_width': 620,
    }
    win._sync_panel_accounts()
    win._relayout()


def expand(win: FloatWindow, to_settings: bool = False) -> None:
    """切到展开态(直接落位,不播动画);to_settings=True 再进设置页。"""
    win._is_expanded = True
    win.detail_panel.setVisible(True)
    if to_settings != win.detail_panel._show_settings:
        win.detail_panel.toggle_settings()
    win._relayout()


def shoot(pm: QPixmap, name: str, edge: str = None,
          pad: int = 48, min_w: int = 720, min_h: int = 520) -> None:
    """把窗口截图合成到一张白色画布上再落盘。

    edge='left'/'right'/'top'/'bottom' 时细条贴画布对应边缘(模拟贴屏幕),
    其余居中摆放。
    """
    pm.setDevicePixelRatio(1.0)   # grab 带 2x 的 DPR 标记,统一按物理像素合成
    w = max(min_w, pm.width() + 2 * pad)
    h = max(min_h, pm.height() + 2 * pad)
    canvas = QPixmap(w, h)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(0, 0, w, h, QColor("#ffffff"))

    if edge == 'left':
        x, y = 0, (h - pm.height()) // 2
    elif edge == 'right':
        x, y = w - pm.width(), (h - pm.height()) // 2
    elif edge == 'top':
        x, y = (w - pm.width()) // 2, 0
    elif edge == 'bottom':
        x, y = (w - pm.width()) // 2, h - pm.height()
    else:
        x, y = (w - pm.width()) // 2, (h - pm.height()) // 2

    painter.drawPixmap(x, y, pm)
    painter.end()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{name}.png"
    canvas.save(str(out))
    print(f"  [ok] {out.relative_to(ROOT)}  ({pm.width()}x{pm.height()})")


def settle(app: QApplication, times: int = 6) -> None:
    """反复泵事件,让布局/字体/二级控件全部就绪再截图。"""
    for _ in range(times):
        app.processEvents()


def snap_dock_geometry(win: FloatWindow) -> None:
    """_dock_to 在"逻辑可见"时会播 200ms 变形动画,截图要的是最终几何:
    停掉动画并直接落到目标位置(等价于动画播完的稳定态)。"""
    win._dock_geometry_animation.stop()
    if win._dock_anim_target.isValid():
        win.setGeometry(QRect(win._dock_anim_target))
    win.update()


def main() -> None:
    app = QApplication(sys.argv)
    # 统一指定微软雅黑(绘制代码只设字号不设族,默认族从这里继承)
    app.setFont(QFont("Microsoft YaHei", 9))

    win = FloatWindow()
    win._apply_identity()
    # 关键:窗口完成真实的布局/绘制,但绝不映射到屏幕 —— 用户看不到任何闪现
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.tray_icon.hide()   # 托盘图标也别真的跑到任务栏去
    win.show()
    settle(app)

    print("生成效果图 ->", OUT_DIR)

    # 01 收起悬浮条-单源(Claude,5h + 7d 两档)
    apply_state(win, MODE_SINGLE, ACCOUNTS, _snapshots())
    settle(app)
    shoot(win.grab(), "01-收起悬浮条-单源")

    # 02 收起悬浮条-多源(五个账号逐行显示)
    apply_state(win, MODE_MULTI, ACCOUNTS, _snapshots())
    settle(app)
    shoot(win.grab(), "02-收起悬浮条-多源")

    # 03 展开-账号列表页(进度条 + 余额 + 重置时间)
    apply_state(win, MODE_MULTI, ACCOUNTS, _snapshots())
    expand(win, to_settings=False)
    settle(app)
    shoot(win.grab(), "03-展开-账号列表页")

    # 04 展开-账号列表页(Key 失效的红色错误态)
    apply_state(win, MODE_MULTI, ACCOUNTS, _snapshots(key_expired=True))
    expand(win, to_settings=False)
    settle(app)
    shoot(win.grab(), "04-展开-账号列表页-Key失效")

    # 05 展开-设置页(显示模式 / 账号勾选 / 开机自启 / 关闭)
    apply_state(win, MODE_MULTI, ACCOUNTS, _snapshots())
    win.detail_panel.set_autostart_checked(True)
    expand(win, to_settings=True)
    settle(app)
    shoot(win.grab(), "05-展开-设置页")

    # 06 贴边收起-左侧竖条(多源:名称 + 数值竖向堆叠)
    apply_state(win, MODE_MULTI, ACCOUNTS, _snapshots())
    settle(app)
    win._dock_to('left')
    snap_dock_geometry(win)
    settle(app)
    shoot(win.grab(), "06-贴边收起-左侧竖条", edge='left')

    # 07 贴边收起-顶部横条(单源经典细条)
    apply_state(win, MODE_SINGLE, ACCOUNTS, _snapshots())
    settle(app)
    win._dock_to('top')
    snap_dock_geometry(win)
    settle(app)
    shoot(win.grab(), "07-贴边收起-顶部横条", edge='top')

    # 08 Cursor 添加账号:Cookie 留空,演示读取本机登录的入口。
    apply_state(win, MODE_SINGLE, ACCOUNTS, _snapshots())
    dlg = AddAccountDialog(win)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dlg.provider_combo.setCurrentIndex(dlg.provider_combo.findData("cursor"))
    dlg.label_input.setText("Cursor")
    dlg.show()
    settle(app)
    shoot(dlg.grab(), "08-添加账号弹窗", pad=32)
    dlg.reject()

    # 09 无账号-悬浮条空态(占位提示)
    apply_state(win, MODE_SINGLE, [], {})
    settle(app)
    shoot(win.grab(), "09-无账号-悬浮条空态")

    # 10 无账号-账号管理引导页(首次启动:展开并进设置页)
    apply_state(win, MODE_SINGLE, [], {})
    expand(win, to_settings=True)
    settle(app)
    shoot(win.grab(), "10-无账号-账号管理引导页")

    # 11 完整高级设置:使用充足的模拟屏幕高度,确保导入导出按钮可见。
    dlg = SettingsDialog(win)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dlg.screen = lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1600, 1600))
    dlg.show()
    settle(app)
    shoot(dlg.grab(), "11-高级设置", pad=20, min_w=0, min_h=0)
    dlg.reject()

    win.close()
    _temporary_data.cleanup()
    print("完成,共 11 张(模拟数据,未访问网络或真实凭据)。")


if __name__ == "__main__":
    main()
