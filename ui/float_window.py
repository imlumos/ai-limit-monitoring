"""
悬浮窗主窗口
==============

【这个文件做什么】
项目的核心 UI 类 FloatWindow。这是一个无边框、置顶、半透明的小悬浮窗,
桌面上常驻显示各 AI 平台额度信息(5h/每周/每月百分比),点击展开后可以填入 API Key、
退出、设置开机自启、关闭服务。

【关键概念:QMainWindow】
QMainWindow 是 Qt 主窗口类,比 QWidget 多了一些"主窗口级"功能
(菜单栏、工具栏、状态栏、托盘等)。本项目用 QMainWindow 作为顶层窗口。

【关键概念:无边框 + 透明 + 置顶】
    Qt.WindowType.FramelessWindowHint         无边框(没有标题栏和系统按钮)
    Qt.WindowType.WindowStaysOnTopHint         永远显示在最上层
    Qt.WindowType.Tool                         不在任务栏出现
    WA_TranslucentBackground                   背景透明
四者组合,造就了"桌面悬浮小卡片"的效果。

【整体结构】
    FloatWindow
        ├── 自己绘制的悬浮条(5h/每周百分比)
        ├── DetailPanel(展开后下方的详情面板)
        ├── QSystemTrayIcon(系统托盘)
        ├── QTimer(定时刷新额度)
        ├── 贴边收起(拖到屏幕边缘自动缩成细条,鼠标靠近自动弹出,离开自动缩回)
        └── 一堆业务逻辑方法(_on_refresh / _on_close_service ...)
"""
import os
import sys
# QtWidgets:窗口和控件
from PySide6.QtWidgets import QMainWindow, QSystemTrayIcon, QMenu, QMessageBox
# QtCore:核心功能(事件、定时器、几何、动画、Qt 枚举)
from PySide6.QtCore import (Qt, QTimer, QPoint, QRect, QRectF, QPropertyAnimation, QSize,
                             QEasingCurve, QByteArray, Slot, QThreadPool)
# QtGui:绘图、字体、图标、屏幕管理
from PySide6.QtGui import (QPainter, QFont, QIcon, QColor, QAction, QGuiApplication,
                           QPixmap, QPainterPath, QPen, QImage)
# 项目内部模块 —— 多平台 / 多账号
import api.providers                       # 导入即注册所有 provider(18 个平台)
from api.models import (QuotaSnapshot, KIND_MONEY, TIER_FIVE_HOUR,
                        TIER_WEEKLY_LIMIT, TIER_SEVEN_DAY, TIER_THIRTY_DAY,
                        TIER_MONTHLY,
                        tier_display_name, tier_short_label,
                        format_money, format_tier_value)
from utils.account_store import AccountStore, AccountStoreError
from utils.ui_state import load_ui_state, save_ui_state, MODE_SINGLE, MODE_MULTI
from ui.poll_manager import PollManager, VerifyCredentialsWorker
from utils.window_manager import WindowManager
from utils.paths import get_resource_path   # 统一处理资源路径(开发/打包兼容)
from utils import autostart
from ui.detail_panel import DetailPanel, AddAccountDialog
from ui.resize_grip import ResizeGrip, EDGES as GRIP_EDGES, grip_rects
from ui.settings_dialog import SettingsDialog
from utils import app_settings
from ui.update_dialog import (UpdateCheckWorker, UpdateDialog,
                              UpdateDownloadWorker, UpdateProgressDialog)
from utils import updater
from utils.version import APP_VERSION
from ui.colors import (get_percentage_color, COLOR_TEXT_WHITE,
                        COLOR_TEXT_SECONDARY, COLOR_TEXT_GRAY)
from typing import List, Optional
# 解析 TOML 配置:Python 3.11+ 标准库 tomllib(项目要求 Python 3.11+)
import tomllib
import logging

# 模块级 logger(自动用 __name__ 命名,这里是 "ui.float_window")
logger = logging.getLogger(__name__)


class FloatWindow(QMainWindow):
    """悬浮窗主窗口"""

    # ====== 贴边收起参数(类似 360/腾讯管家的贴边条) ======
    # 尺寸分两层:常量只定"单源"的基准;多源时 _docked_collapsed_size()
    # 按可见账号数动态加宽/加高,保证每个账号的圆点+名称+数值都放得下。
    DOCK_THRESHOLD = 24      # 拖动松手时,窗口距屏幕边缘小于该像素数即触发贴边
    DOCK_MINI_W = 150        # 贴边后水平细条(上/下边缘)的宽度 —— 单源用
    DOCK_MINI_H = 34         # 贴边后水平细条的高度 —— 所有横条的高度下限
    DOCK_SIDE_W = 88         # 贴边后竖直细条(左/右边缘)的宽度
    #   88 的由来:圆点 8 + 间距 4 + "DeepSeek"@8pt ≈ 55,数值行@9pt ≈ 50,
    #   加左右内边距 12 合计 ≤ 79,给 Windows 125%/150% DPI 留 10-20% 余量。
    DOCK_STRIP_H = 150       # 贴边后竖直细条的高度 —— 单源用(容纳两行指标)
    DOCK_MULTI_MINI_W = 200  # 多源贴顶细条宽(名称+数值一行放得下)
    DOCK_MULTI_ROW_H = 20    # 多源贴顶:每个账号一行的高度
    DOCK_MULTI_VPAD = 8      # 多源细条:上下内边距合计
    DOCK_SLIDE_MS = 200      # 松手贴边/拖离时的变形动画时长(毫秒)
    TOGGLE_H = 18            # 悬浮条底部留给展开/收起按钮的高度(自由态)
    TOGGLE_W = 40            # 展开/收起按钮的点击宽度(水平居中)

    # 用户拖拽改大小的范围
    MIN_WIDTH = 220
    MAX_WIDTH = 800
    MIN_PANEL_H = 120

    def __init__(self):
        """初始化:配置、网络客户端、UI、定时器、托盘等

        【为什么拆成一堆 _init_xxx】
            原本这里塞了 11 个步骤,70+ 行,新人很难一眼看懂。
            拆成多个职责清晰的小方法后,本函数只负责"按顺序调用",
            具体逻辑去对应方法里看。
        """
        super().__init__()

        # 1) 加载配置(读 settings.toml)
        self.config = self._load_config()
        self._reload_background()

        # 2) 业务对象(账号仓库、UI 状态、快照缓存)
        self._init_business_objects()

        # 3) 拖动状态
        self._init_drag_state()

        # 4) 展开状态
        self._is_expanded = False    # 当前是否展开详情面板
        self._is_animating = False   # 当前是否在播放展开/折叠动画

        # 5) 展开/折叠动画
        self._init_animations()

        # 6) 详情面板
        self._init_detail_panel()

        # 7) UI(尺寸、无边框、透明、位置)
        self._init_ui()

        # 8) 定时器(额度刷新)
        self._init_timers()

        # 9) 系统托盘
        self._setup_tray()

        # 10) 同步开机自启复选框(从注册表读真实状态)
        self.detail_panel.set_autostart_checked(autostart.is_enabled())

        # 11) 屏幕变化监听(切分辨率/DPI/拔插屏时校准窗口位置)
        self._init_screen_watchers()

    def _init_business_objects(self):
        """初始化业务对象:账号仓库、UI 状态、快照缓存(替代旧的单 GLMClient)"""
        # 多账号仓库(首次运行会自动把旧版单个 GLM Key 迁移成 migrated-glm 账号)
        self.account_store = AccountStore()
        # 显示模式 + 账号选择(存 data/ui_state.json)
        self.ui_state = load_ui_state()
        # 最新快照:account_id -> QuotaSnapshot(主窗口缓存,绘制悬浮条/列表用)
        self._snapshots = {}
        # 验证新账号用的 worker 引用(持有以防被 GC)
        self._verify_worker = None

    def _init_drag_state(self):
        """初始化拖动相关状态(鼠标按下/移动/释放时使用)"""
        # QPoint() 默认构造 (0, 0) 的点
        self._drag_position = QPoint()
        self._is_dragging = False
        self._click_start_pos = QPoint()
        self._click_threshold = 10  # 移动超过 10 像素视为"拖动"而非"点击"

        # ====== 贴边收起状态 ======
        self._docked = False          # 是否处于贴边收起模式
        self._dock_edge = None        # 贴在哪条边:'left'/'right'/'top'/'bottom'
        self._free_geometry = QRect() # 贴边前(自由拖动时)的几何信息,保存位置用它
        # 变形动画当前的目标几何(去重用:目标没变就不重启动画,防周期刷新抖动)
        self._dock_anim_target = QRect()

    def _init_animations(self):
        """初始化展开/折叠的高度动画"""
        # QPropertyAnimation:对 QObject 的属性做补间动画
        # 这里对 self 的 "size" 属性做动画,实现展开/折叠时高度平滑变化
        self._height_animation = QPropertyAnimation(self, b"size")
        self._height_animation.setDuration(250)                          # 动画时长 250ms
        self._height_animation.setEasingCurve(QEasingCurve.Type.OutCubic)  # 缓动曲线(快进慢出)
        # 动画结束时触发 _on_animation_finished
        self._height_animation.finished.connect(self._on_animation_finished)

        # 贴边/拖离时的变形动画:对 "geometry" 做动画,实现松手时平滑变大变小
        self._dock_geometry_animation = QPropertyAnimation(self, b"geometry")
        self._dock_geometry_animation.setDuration(self.DOCK_SLIDE_MS)
        self._dock_geometry_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _init_detail_panel(self):
        """创建详情面板并连接它的信号到本窗口的槽函数"""
        # 把本窗口作为 DetailPanel 的父对象
        self.detail_panel = DetailPanel(self)

        # 多账号信号(替代旧版 login/logout)
        self.detail_panel.add_requested.connect(self._on_add_requested)
        self.detail_panel.delete_account_clicked.connect(self._on_delete_account)
        self.detail_panel.mode_changed.connect(self._on_mode_changed)
        self.detail_panel.selection_changed.connect(self._on_selection_changed)
        self.detail_panel.relayout_requested.connect(self._relayout)
        self.detail_panel.close_clicked.connect(self._on_close_service)
        self.detail_panel.autostart_toggled.connect(self._on_autostart_toggled)
        self.detail_panel.account_refresh_clicked.connect(self._on_account_refresh)
        self.detail_panel.accounts_reordered.connect(self._on_accounts_reordered)
        self.detail_panel.rename_requested.connect(self._on_rename_account)
        self.detail_panel.advanced_settings_clicked.connect(self._open_settings_dialog)

        # 推送初始账号列表 + 当前选择状态给面板
        self._sync_panel_accounts()

        # 边缘拖拽改大小的手柄(盖在面板之上,位置/显隐由 _update_grips 维护)
        self._grips = {edge: ResizeGrip(self, edge) for edge in GRIP_EDGES}
        self._resize_start = None   # 拖拽开始时的 (geometry, 面板高)

    def _init_timers(self):
        """初始化轮询管理器:一个共享定时器 + 线程池,刷新所有启用账号"""
        interval_ms = self.config['refresh']['interval_minutes'] * 60 * 1000
        self.poll_manager = PollManager(self.account_store, interval_ms, parent=self)
        self.poll_manager.snapshots_ready.connect(self._on_snapshots)

    def _load_config(self) -> dict:
        """内置默认配置 + data/settings.toml 里用户在设置界面改过的值"""
        return app_settings.merge(self._load_bundled_config(),
                                  app_settings.load_user_settings())

    def _load_bundled_config(self) -> dict:
        """加载打包内置的 TOML 配置文件(路径推算委托给 utils.paths)"""
        # 打包后从 sys._MEIPASS,开发环境从项目根目录读
        config_path = get_resource_path(os.path.join('config', 'settings.toml'))

        try:
            # 'rb' 二进制读模式(tomllib/tomli 都要求二进制)
            with open(config_path, 'rb') as f:
                return tomllib.load(f)
        except Exception as e:
            # 配置读失败时给一份默认值,保证程序至少能跑起来
            logger.warning("加载配置文件失败: %s (path=%s)", e, config_path, exc_info=True)
            return {
                'window': {'width': 260, 'height': 50, 'corner_radius': 25},
                'refresh': {'interval_minutes': 10},
                'update': {'enabled': False, 'check_on_startup': False},
                'appearance': {'bg_color': '#000000', 'bg_opacity': 70, 'bg_image': ''},
            }

    def _init_ui(self):
        """初始化 UI:窗口尺寸、属性、位置"""
        base_width = self._base_width()
        base_height = self.config['window']['height']
        # 初始尺寸按当前显示模式(多源模式条会更高);_relayout 之后会再校准
        self.resize(base_width, self._current_bar_height())
        # 不用 setFixedWidth:贴边收起时宽度要能自由缩小成细条;
        # 用户拖边缘改大小走 ResizeGrip → _user_resize,宽度仍由代码统一控制
        self.setMinimumHeight(base_height)   # 最小高度 = 单行基准(50),作为地板
        self.setMaximumHeight(1000)          # 上限足够展开详情面板(多账号可滚动)
        self.setMouseTracking(True)          # 悬停展开/收起按钮时显示手形光标

        # 无边框 + 置顶 + 不在任务栏 + 透明背景(详见模块顶部说明)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # 还原上次位置;失败或屏幕外则回退居中
        # 详细逻辑见 _restore_or_center
        self._restore_or_center()

        # 详情面板初始隐藏(用户点击展开后才显示)
        self.detail_panel.setVisible(False)

    def _center_window(self):
        """窗口居中(主屏幕,扣掉任务栏)"""
        screen = self.screen().availableGeometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def _restore_or_center(self):
        """启动时还原上次位置;失败或窗口在屏幕外则居中;上次贴边则恢复吸附

        【策略】
            1. 从 WindowManager 读取上次保存的几何信息
            2. 用 Qt 自带的 restoreGeometry 还原
            3. 再做一次屏幕外兜底检查(防止分辨率变化/外接屏拔了)
            4. 任何环节失败都回退到居中
            5. 上次退出时处于贴边状态 -> 恢复吸附(细条贴在原边缘)

        【为什么不在退出时才保存】
            本项目的关闭流程用 os._exit(0) 强制退出,不会触发 closeEvent。
            所以位置和贴边状态都在用户拖动结束时立即写入,见 mouseReleaseEvent。
        """
        geometry_bytes = WindowManager.load_geometry()
        if geometry_bytes:
            # restoreGeometry 返回 True 表示数据有效(Qt 自带基础校验)
            if self.restoreGeometry(QByteArray(geometry_bytes)):
                # Qt 校验不够,补充屏幕外检查
                if self._is_visible_on_any_screen(self.frameGeometry()):
                    # 上次贴边过 -> 恢复吸附状态(位置记录的是贴边前的自由位置)
                    dock_edge = WindowManager.load_dock_edge()
                    if dock_edge in ('left', 'right', 'top', 'bottom'):
                        self._dock_to(dock_edge)
                    return
                # 上次的位置已经不可见(拔屏/分辨率变化),直接清除贴边状态,
                # 否则 json 里残留 dock_edge,下次启动又重复走"居中但贴边"的分支
                self._center_window()
                WindowManager.save_geometry(self.saveGeometry(), dock_edge=None)
                return
        # 无保存数据 / restoreGeometry 失败 / 窗口不可见:回退到居中
        self._center_window()

    @staticmethod
    def _is_visible_on_any_screen(geom: QRect) -> bool:
        """窗口是否在任一屏幕的可用区域内"足够可见"

        Args:
            geom: 窗口的几何矩形(QRect)

        【为什么要求足够可见而不是任意相交】
            用 intersects 判断太宽松:窗口哪怕只有 1px 搭在屏上就算"可见",
            拔掉 2K 屏/分辨率变化后窗口可能大面积出屏却不触发居中兜底。
            这里要求交集面积 >= 窗口面积的 30%(防误伤:用户可能故意把
            窗口拖到两屏之间,只要求主体可见)。
        """
        area = max(geom.width(), 1) * max(geom.height(), 1)
        for screen in QGuiApplication.screens():
            inter = screen.availableGeometry().intersected(geom)
            if inter.width() <= 0 or inter.height() <= 0:
                continue
            if inter.width() * inter.height() >= area * 0.3:
                return True
        return False

    def _clamp_into_screen(self, geom: QRect):
        """把窗口整体拉回"交集最大的屏幕"的可用区内(坐标越界就 clamp)

        用于屏幕拔插/分辨率变化后,把出屏的窗口拉回可见位置。
        """
        best = None
        best_area = -1
        for screen in QGuiApplication.screens():
            avail = screen.availableGeometry()
            inter = avail.intersected(geom)
            area = inter.width() * inter.height()
            if area > best_area:
                best, best_area = avail, area
        if best is None:
            best = QGuiApplication.primaryScreen().availableGeometry()
        if geom.width() >= best.width():
            x = best.left()
        else:
            x = min(max(geom.x(), best.left()), best.right() - geom.width() + 1)
        if geom.height() >= best.height():
            y = best.top()
        else:
            y = min(max(geom.y(), best.top()), best.bottom() - geom.height() + 1)
        self.move(x, y)

    def _init_screen_watchers(self):
        """监听屏幕变化,变化时校准窗口位置

        【覆盖的场景】
            - 分辨率变化(2K -> 1080p):QScreen.geometryChanged
            - DPI 缩放变化(150% -> 100%):QScreen.logicalDotsPerInchChanged
            - 任务栏变化(可用区域变了):QScreen.availableGeometryChanged
            - 拔插屏幕/换主屏:QGuiApplication.screenAdded/screenRemoved/primaryScreenChanged
            - 窗口被系统挪到别的屏:QWindow.screenChanged

        【为什么窗口自身换屏也要监听】
            拔掉副屏后 Qt/Windows 会把窗口自动挪到剩余屏幕,此时屏幕几何
            没变、只是窗口换了屏,窗口可能落在残影坐标上,需要再校准一次。
        """
        app = QGuiApplication.instance()
        app.screenAdded.connect(self._on_screens_changed)
        app.screenRemoved.connect(self._on_screens_changed)
        app.primaryScreenChanged.connect(self._on_screens_changed)
        # 已连接过信号的屏幕 id 集合,防止重复 connect 导致回调重复触发
        self._watched_screens = set()
        for screen in app.screens():
            self._connect_screen_signals(screen)
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._on_screens_changed)

        # 校准防抖 + 延迟执行:
        # Qt 处理 DPI 变化/换屏是异步的,信号触发那一刻窗口的尺寸/DPR
        # 可能还没被 Qt 改完,立即校准会看到"旧尺寸",resize 后又被 Qt
        # 后续处理覆盖(表现为窗口突然变大变小、拖一下才正常)。
        # 所以信号回调只重启这个单发定时器,等 Qt 处理完再校准。
        self._calibrate_timer = QTimer(self)
        self._calibrate_timer.setSingleShot(True)
        self._calibrate_timer.setInterval(300)
        self._calibrate_timer.timeout.connect(self._calibrate_position)

        # 复核定时器:Qt 的处理可能比防抖延迟更晚,校准后再复查一次,
        # 尺寸仍不对就再校准(幂等,Qt 处理完后不会反复改,会收敛)
        self._verify_timer = QTimer(self)
        self._verify_timer.setSingleShot(True)
        self._verify_timer.setInterval(600)
        self._verify_timer.timeout.connect(self._on_verify_timeout)

    def _connect_screen_signals(self, screen):
        """给一个屏幕连接变化信号(同一个屏幕只连一次)"""
        if id(screen) in self._watched_screens:
            return
        self._watched_screens.add(id(screen))
        screen.geometryChanged.connect(self._on_screens_changed)
        screen.availableGeometryChanged.connect(self._on_screens_changed)
        screen.logicalDotsPerInchChanged.connect(self._on_screens_changed)

    def _on_screens_changed(self, *args):
        """屏幕变化的统一入口:给新屏补连信号,延迟校准窗口位置

        高频信号(geometryChanged/DPI变化会连发多次)合并成一次校准:
        只重启定时器,不立即执行。
        """
        for screen in QGuiApplication.screens():
            self._connect_screen_signals(screen)
        self._calibrate_timer.start()
        self._verify_timer.stop()

    def _is_mostly_visible(self, geom: QRect) -> bool:
        """窗口主体(>=50%)是否在任一屏幕的可用区域内

        比 _is_visible_on_any_screen(30%)更严格:
        大屏切小屏时窗口可能停在旧位置,新屏只盖住它一小条,
        30% 阈值会误判"可见",窗口实际大半截被切掉。
        """
        area = max(geom.width(), 1) * max(geom.height(), 1)
        for screen in QGuiApplication.screens():
            inter = screen.availableGeometry().intersected(geom)
            if inter.width() <= 0 or inter.height() <= 0:
                continue
            if inter.width() * inter.height() >= area * 0.5:
                return True
        return False

    def _on_verify_timeout(self):
        """复核:校准后若 Qt 又改了尺寸/位置,再校准一次;改了才继续复查"""
        if self._calibrate_position():
            self._verify_timer.start()

    def _calibrate_position(self) -> bool:
        """屏幕变化后校准窗口位置(幂等,可被高频触发)

        - 贴边中:用新的 availableGeometry 重算细条位置;
          新屏幕容不下细条(理论上极罕见)则取消贴边
        - 自由状态:强制恢复基准尺寸;窗口主体被新屏切掉
          (>=50% 出屏)则拉回屏内

        Returns:
            True 表示本次做了修改(尺寸/位置变了)
        """
        if not self.isVisible():
            return False
        if self._docked and self._dock_edge:
            target = self._docked_collapsed_geometry()
            visible = any(s.availableGeometry().intersects(target)
                          for s in QGuiApplication.screens())
            if visible:
                # 细条还在屏内:直接重贴(不播动画,切屏瞬间动画没意义)
                self._dock_geometry_animation.stop()
                if self.geometry() != target:
                    self.setGeometry(target)
                    self.update()
                    # 贴边细条同样可能有"右半边消失"的窗口矩形残留
                    self._force_refresh_window()
                    return True
            else:
                # 新屏容不下细条:取消贴边,完整窗口拉回屏内
                self._undock()
                self.resize(self._base_width(),
                            self.config['window']['height'])
                self._clamp_into_screen(self.frameGeometry())
                # 清除贴边状态,避免下次启动恢复吸附到错误位置
                WindowManager.save_geometry(self.saveGeometry(), dock_edge=None)
                return True
            return False

        # 自由状态:
        # 1) 强制恢复基准尺寸。Qt 在屏幕 DPI 变化(2K@150% -> 1080p@100%)
        #    或窗口换屏时会按"保持物理像素"重算窗口,导致逻辑尺寸突变,
        #    窗口看起来突然变大/变小;这里把它拉回基准尺寸。展开时按
        #    展开高度恢复。
        base_w = self._base_width()
        base_h = self._current_bar_height()
        if self._is_expanded:
            target_h = base_h + self._panel_height()
        else:
            target_h = base_h
        if self.width() != base_w or self.height() != target_h:
            self.resize(base_w, target_h)
            self.detail_panel.setGeometry(0, base_h, base_w, target_h - base_h)
            self.update()
            changed = True
        else:
            changed = False
        # 2) 窗口主体(>=50%)被新屏切掉 -> 拉回屏内。
        #    大屏切小屏时窗口可能停在旧位置,新屏只盖住一小条,
        #    表现为"截断一半",必须强制拉回。
        if not self._is_mostly_visible(self.frameGeometry()):
            self._clamp_into_screen(self.frameGeometry())
            changed = True
        # 3) 强制刷新窗口系统矩形:DPI 变化后 Windows 的窗口矩形可能
        #    停留在旧尺寸,内容只画出一部分(窗口在中间,右半边凭空
        #    消失;拖拽一下才恢复)。校准完主动抖动一次几何,等效于
        #    替用户"拖拽一下"。
        if changed:
            self._force_refresh_window()
            self._verify_timer.start()
        return changed

    def _force_refresh_window(self):
        """强制刷新窗口系统矩形/渲染缓冲

        【解决什么问题】
            屏幕 DPI 变化(2K@150% -> 1080p@100%)后,Windows 对窗口的
            系统矩形可能还停留在旧尺寸,而 Qt 渲染缓冲已按新 DPR 重建,
            导致窗口只画出其中一部分(右半边空白)。手动 move 一次会触发
            SetWindowPos 让系统更新矩形——这就是"拖拽一下能回来"的机制。
        """
        if not self.isVisible():
            return
        # 记住原始位置:50ms 后移回(抖动期间窗口可能被用户拖动/动画移动)
        self._shake_origin = (self.pos().x(), self.pos().y())
        # 先向右抖 1px,50ms 后再移回(1px 位移视觉无感,但能强制
        # 窗口系统刷新矩形;分两次事件循环,避免 SetWindowPos 被合并)
        self.move(self._shake_origin[0] + 1, self._shake_origin[1])
        QTimer.singleShot(50, self._restore_shake_position)

    def _restore_shake_position(self):
        """抖动画几何后把位置移回原处"""
        if not self.isVisible():
            return
        origin = getattr(self, '_shake_origin', None)
        if origin is not None:
            self.move(origin[0], origin[1])
        self.update()

    # ============================================================
    # 贴边收起(类似 360/腾讯管家的贴边条)
    #   拖动窗口到屏幕边缘松手 -> 自动缩小成屏幕边缘的一条细条
    #   (细条是原窗口的缩小版:颜色、圆点、数字都保留,间隔变小);
    #   贴边后悬停/点击都不会变形,从细条上拖离边缘松手
    #   -> 恢复完整大小。
    # ============================================================

    def _maybe_dock(self):
        """当前窗口位置是否贴近屏幕边缘,是则返回贴边方向('left'/'right'/'top'/'bottom'),否则 None"""
        avail = self.screen().availableGeometry()
        pos = self.pos()
        w, h = self.width(), self.height()
        distances = {
            'left':   pos.x() - avail.left(),
            'right':  avail.right() - (pos.x() + w),
            'top':    pos.y() - avail.top(),
            'bottom': avail.bottom() - (pos.y() + h),
        }
        # 取离窗口最近的一条边(窗口已经出屏幕时距离为负,必中)
        edge = min(distances, key=distances.get)
        if distances[edge] > self.DOCK_THRESHOLD:
            return None
        return edge

    # 侧栏卡片行高(与 _draw_vertical_content 的 name_h/chip_h 保持一致)
    SIDE_NAME_H = 14    # 名称行高
    SIDE_CHIP_H = 16    # 每个 chip 行高

    def _side_card_height(self, account_id: str) -> int:
        """一个账号在侧栏的卡片高度:名称行 + 实际 chip 行数 × chip 行高

        chips 数由 _bar_chips 按快照决定(0~2 个;无档位兜底 "--" 1 行),
        所以双档账号卡片 46px、单档 30px —— 高度跟着"要展示的内容"走。
        """
        chips = self._bar_chips(self._snapshots.get(account_id))
        return self.SIDE_NAME_H + max(1, len(chips)) * self.SIDE_CHIP_H

    def _docked_collapsed_size(self) -> tuple:
        """按 模式 × 可见账号数 × 屏幕工作区 计算吸附条的 (宽, 高)

        【为什么做成纯函数】_calibrate_position(切屏/DPI 变化)也会经
        _docked_collapsed_geometry 间接触发本函数。同状态下必须返回同一结果
        (幂等),否则校准会来回跳;尺寸只依赖当前状态,不产生副作用。

        【侧边高度为什么逐账号算】卡片高度取决于该账号有几个档位
        (双档 46px / 单档 30px),按 n×固定块高会把双档卡片挤出条外。
        """
        avail = self.screen().availableGeometry()
        vertical = self._dock_edge in ('left', 'right')
        is_multi = self.ui_state.get('display_mode') == MODE_MULTI
        n = max(1, len(self._visible_account_ids()))

        if vertical:
            # 侧边:固定 88 宽;单源沿用 150 高,多源按每账号实际卡片高累加
            w = self.DOCK_SIDE_W
            if not is_multi:
                h = self.DOCK_STRIP_H
            else:
                accounts = {a.id: a for a in self.account_store.list_accounts()}
                h = self.DOCK_MULTI_VPAD + sum(
                    self._side_card_height(i)
                    for i in self._visible_account_ids() if i in accounts)
            h = min(h, avail.height() - 2 * self.DOCK_THRESHOLD)
        elif not is_multi:
            # 单源贴顶:与旧版完全一致,作为回归基准不动
            w, h = self.DOCK_MINI_W, self.DOCK_MINI_H
        else:
            # 多源贴顶:每账号一行,高度自增;n=1 时仍保住 34 的地板值,
            # 否则 8+20=28 会被 setMinimumHeight(34) 卡住缩不下去
            w = self.DOCK_MULTI_MINI_W
            h = max(self.DOCK_MINI_H,
                    self.DOCK_MULTI_VPAD + n * self.DOCK_MULTI_ROW_H)
            w = min(w, avail.width() - 2 * self.DOCK_THRESHOLD)
            h = min(h, avail.height() - 2 * self.DOCK_THRESHOLD)
        return w, h

    def _docked_collapsed_geometry(self) -> QRect:
        """贴边收起时的几何:缩小成细条,紧贴屏幕边缘
        - 左/右边缘:竖直细条(窄而高),内容上下堆叠
        - 上/下边缘:水平细条(矮而宽),内容横排

        尺寸交给 _docked_collapsed_size() 按模式/账号数动态算;
        本函数只负责"贴到哪条边、锚点怎么定":
        - 上下边锚定自由态的 x,左右边锚定自由态的 y(吸附那一刻的位置)
        - 贴底/贴右时坐标从右/下边缘反推,高度变化时自动向上/向左生长
        """
        avail = self.screen().availableGeometry()
        w, h = self._docked_collapsed_size()
        fx = self._free_geometry.x()
        fy = self._free_geometry.y()
        if self._dock_edge == 'left':
            y = min(max(fy, avail.top()), avail.bottom() - h)
            return QRect(avail.left(), y, w, h)
        if self._dock_edge == 'right':
            y = min(max(fy, avail.top()), avail.bottom() - h)
            return QRect(avail.right() - w + 1, y, w, h)
        if self._dock_edge == 'top':
            x = min(max(fx, avail.left()), avail.right() - w)
            return QRect(x, avail.top(), w, h)
        x = min(max(fx, avail.left()), avail.right() - w)
        return QRect(x, avail.bottom() - h + 1, w, h)

    def _undock(self):
        """取消贴边:恢复自由模式(最小/最大高度也恢复,允许窗口变回完整大小)"""
        self._dock_geometry_animation.stop()
        self._docked = False
        self._dock_edge = None
        self.setMinimumHeight(self.config['window']['height'])
        self.setMaximumHeight(1000)   # 与 _init_ui 的初始上限保持一致
        self.update()

    def _animate_geometry(self, target: QRect):
        """把窗口平滑变形到 target(松手贴边/拖离时用)

        目标同时记到 _dock_anim_target:吸附态周期刷新(_on_snapshots →
        _relayout_docked)靠比较它来判断"尺寸没变,不用重播动画"。
        """
        self._dock_geometry_animation.stop()
        self._dock_anim_target = QRect(target)
        self._dock_geometry_animation.setStartValue(self.geometry())
        self._dock_geometry_animation.setEndValue(target)
        self._dock_geometry_animation.start()

    def _dock_to(self, edge: str):
        """执行贴边:记录自由位置、保存窗口位置和贴边状态,然后缩小成细条贴到边缘

        运行时(松手)播放变形动画;启动恢复吸附时不播(窗口还没显示,动画无意义)
        """
        self._free_geometry = QRect(self.pos(), self.size())

        # 保存自由位置(下次启动恢复到这里再重新贴边,不会直接是细条状态)
        # 同时记录贴边状态,重启后自动恢复吸附
        WindowManager.save_geometry(self.saveGeometry(), dock_edge=edge)

        self._docked = True
        self._dock_edge = edge
        # 贴边期间禁止展开详情面板(细条 + 面板的尺寸逻辑冲突)
        self._height_animation.stop()
        self._is_animating = False
        self._is_expanded = False
        self.detail_panel.setVisible(False)
        # 放低最小高度限制,允许缩成细条。
        # 多源 n=1 时目标高是 34(地板值),恰好等于 DOCK_MINI_H;
        # 用 min() 是防御未来高度算出 <34 时不会被最小高卡住缩不下去
        _, h = self._docked_collapsed_size()
        self.setMinimumHeight(min(self.DOCK_MINI_H, h))
        # 最大高度抬到屏幕工作区:侧边多源细条高 8+n*38,账号多时可能
        # 超过 _init_ui 设的 1000 上限被 Qt 钳住 → geometry 永远到不了
        # target,_relayout_docked 每轮刷新都会重播动画(周期抽搐)
        self.setMaximumHeight(self.screen().availableGeometry().height())
        target = self._docked_collapsed_geometry()
        if self.isVisible():
            self._animate_geometry(target)
        else:
            self.setGeometry(target)
        self.update()

    def _setup_tray(self):
        """设置系统托盘图标和右键菜单"""
        # QSystemTrayIcon:系统托盘图标
        self.tray_icon = QSystemTrayIcon(self)

        self._rebuild_tray_menu()
        self._apply_identity()
        self.tray_icon.show()

    def _apply_identity(self):
        identity = self.config.get("application", {})
        name = identity.get("name", app_settings.DEFAULT_APP_NAME)
        # 经 QImage 读取以绕过 Qt 的文件路径缓存,同名图标替换后也立即刷新。
        pixmap = QPixmap.fromImage(QImage(app_settings.app_icon_path(identity.get("icon", ""))))
        icon = QIcon(get_resource_path("favicon.ico")) if pixmap.isNull() else QIcon(pixmap)
        app = QGuiApplication.instance()
        app.setApplicationName(name)
        app.setApplicationDisplayName(name)
        app.setWindowIcon(icon)
        self.setWindowTitle(name)
        self.setWindowIcon(icon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip(name)

    def _rebuild_tray_menu(self):
        """(重新)生成托盘右键菜单:更新开关在设置里改了之后要重建"""
        tray_menu = QMenu()

        # 检查更新(开发环境默认隐藏,和开机自启同一个 frozen 门禁思路)
        if self.config.get('update', {}).get('enabled', False) and updater.update_supported():
            check_update_action = QAction("检查更新", self)
            check_update_action.triggered.connect(
                lambda: self._check_for_updates(manual=True))
            tray_menu.addAction(check_update_action)
            tray_menu.addSeparator()

        reset_size_action = QAction("恢复默认大小", self)
        reset_size_action.triggered.connect(self._reset_window_size)
        tray_menu.addAction(reset_size_action)
        tray_menu.addSeparator()

        # QAction 是一个菜单项
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._on_close_service)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self._tray_menu = tray_menu   # 持有引用:setContextMenu 不接管所有权

    # ============================================================
    # 多账号:选择 / 高度 / 列表同步
    # ============================================================
    def _enabled_account_ids(self) -> List[str]:
        return [a.id for a in self.account_store.list_enabled()]

    def _visible_account_ids(self) -> List[str]:
        """计算当前要在悬浮条上显示的账号 id 列表"""
        enabled = self._enabled_account_ids()
        if self.ui_state.get('display_mode') == MODE_SINGLE:
            active = self.ui_state.get('active_account_id')
            if active in enabled:
                return [active]
            return enabled[:1]   # 没指定就默认第一个
        # 多源:visible 为空(未配置过)→ 显示全部启用;否则按勾选
        # 按账号存储顺序(用户拖拽排的序),而非勾选时记下的顺序
        checked = set(self.ui_state.get('visible_account_ids') or [])
        vis = [i for i in enabled if i in checked]
        return vis if vis else enabled

    def _current_bar_height(self) -> int:
        """收起态悬浮条高度 = 内容区 + 底部展开/收起按钮行"""
        return self._bar_content_height() + self.TOGGLE_H

    def _bar_content_height(self) -> int:
        """悬浮条内容区高度:单源=基准高;多源=表头 + 可见行数×行高(带上限)"""
        base = self.config['window']['height']
        if self.ui_state.get('display_mode') == MODE_MULTI:
            n = max(1, len(self._visible_account_ids()))
            row_h, top_pad = 22, 8
            return min(base + top_pad + n * row_h, base * 8)
        return base

    def _toggle_rect(self) -> QRect:
        """展开/收起按钮区域:悬浮条底部按钮行、水平居中(仅自由态有)"""
        return QRect((self.width() - self.TOGGLE_W) // 2, self._bar_content_height(),
                     self.TOGGLE_W, self.TOGGLE_H)

    def _draw_toggle_button(self, painter: QPainter):
        """收起态画向下箭头(点了展开),展开态画向上箭头(点了收起)"""
        r = self._toggle_rect()
        cx, cy = r.center().x(), r.center().y()
        d = -3 if self._is_expanded else 3   # 箭头尖朝上/朝下
        painter.save()
        painter.setPen(QPen(QColor(200, 200, 200), 2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - 8, cy - d)
        path.lineTo(cx, cy + d)
        path.lineTo(cx + 8, cy - d)
        painter.drawPath(path)
        painter.restore()

    def _sync_panel_accounts(self):
        """把账号列表 + 当前 ui_state 推给详情面板"""
        self.detail_panel.set_accounts(self.account_store.list_accounts(), self.ui_state)

    def _relayout(self):
        """按当前状态重排窗口尺寸:展开=条高+面板高;收起=条高;贴边=重算细条尺寸。

        详情面板不要滚动条,内容多了就整体长高——所以这里每次都按面板的
        get_required_height() 重算窗口高度(账号/档位/页面变了都会触发)。
        """
        if self._is_animating:
            self.update()
            return
        if self._docked:
            # 吸附态也要重排:账号数/模式变化时细条尺寸要跟着变
            # (启动恢复吸附时 startup_flow 会修剪陈旧账号 id,修剪完走这里校正)
            self._relayout_docked()
            self.update()
            return
        bar_h = self._current_bar_height()
        if self._is_expanded:
            panel_h = self._panel_height()
            target = bar_h + panel_h
            self.setMaximumHeight(target)
            self.resize(self._base_width(), target)
            self.detail_panel.setGeometry(0, bar_h, self.width(), panel_h)
            self.detail_panel.update()
        else:
            # 收起态:宽度或高度任一偏离基准就 resize。宽度也要保证 ——
            # restoreGeometry 可能恢复上次的窄宽度,而单源时高度恰好等于基准(50),
            # 若只比高度会漏掉宽度,导致启动时悬浮窗左右很窄、拖一下才恢复。
            if self.width() != self._base_width() or self.height() != bar_h:
                self.resize(self._base_width(), bar_h)
        self._update_grips()
        self.update()

    # ============================================================
    # 用户拖拽改大小(宽度:收起/展开都可调;高度:仅展开态的账号列表页)
    # ============================================================
    def _base_width(self) -> int:
        """自由态窗口宽度:用户拖过就用拖出的宽度,否则用配置默认宽度"""
        w = self.ui_state.get('window_width')
        if isinstance(w, int) and not isinstance(w, bool):
            return min(max(w, self.MIN_WIDTH), self.MAX_WIDTH)
        return self.config['window']['width']

    def _panel_height(self) -> int:
        """展开态面板高度:列表页用用户拖出的高度(内容超出则面板内滚动);
        未拖过或在设置页时随内容(设置页是 Qt 布局,压矮会挤坏控件)"""
        h = self.ui_state.get('panel_height')
        if (self.detail_panel.showing_settings
                or not isinstance(h, int) or isinstance(h, bool)):
            return self.detail_panel.get_required_height()
        return max(h, self.MIN_PANEL_H)

    def _update_grips(self):
        """按当前状态摆放手柄:贴边时全隐藏;下边/下角仅展开态列表页可用"""
        if not hasattr(self, '_grips'):
            return
        bottom = self._is_expanded and not self.detail_panel.showing_settings
        rects = {} if self._docked else grip_rects(self.width(), self.height(), bottom)
        for edge, grip in self._grips.items():
            r = rects.get(edge)
            grip.setVisible(r is not None)
            if r is not None:
                grip.setGeometry(*r)
                grip.raise_()

    def _begin_user_resize(self):
        self._height_animation.stop()
        self._is_animating = False
        panel_h = self.detail_panel.height() if self._is_expanded else None
        self._resize_start = (QRect(self.geometry()), panel_h)

    def _user_resize(self, edge: str, dx: int, dy: int):
        if self._resize_start is None or self._docked:
            return
        geom, panel_h = self._resize_start
        avail = self.screen().availableGeometry()
        if edge in ('left', 'bottomleft'):
            # 左边:右边缘不动,宽度反向变化;不超出屏幕左缘
            max_w = min(self.MAX_WIDTH, geom.right() + 1 - avail.left())
            self.ui_state['window_width'] = min(max(geom.width() - dx, self.MIN_WIDTH), max_w)
        elif edge in ('right', 'bottomright'):
            max_w = min(self.MAX_WIDTH, avail.right() + 1 - geom.left())
            self.ui_state['window_width'] = min(max(geom.width() + dx, self.MIN_WIDTH), max_w)
        if edge.startswith('bottom') and panel_h is not None:
            # 底边不超出屏幕工作区
            max_h = max(self.MIN_PANEL_H,
                        avail.bottom() + 1 - geom.top() - self._current_bar_height())
            self.ui_state['panel_height'] = min(max(panel_h + dy, self.MIN_PANEL_H), max_h)
        self._relayout()
        if edge in ('left', 'bottomleft'):
            self.move(geom.right() + 1 - self.width(), geom.top())

    def _end_user_resize(self):
        if self._resize_start is None:
            return
        self._resize_start = None
        save_ui_state(self.ui_state)
        # 拖左边会改变位置,一并保存(与拖动结束保存位置同理)
        WindowManager.save_geometry(self.saveGeometry(), dock_edge=None)

    # ============================================================
    # 高级设置(刷新间隔 / 更新策略;代理由对话框直接存 proxy.toml)
    # ============================================================
    def _open_settings_dialog(self):
        SettingsDialog(self).exec()

    def apply_settings(self, overrides: dict):
        """校验 → 写 data/settings.toml → 更新内存配置并立即生效。非法抛 SettingsError,什么都不改。"""
        merged = app_settings.merge(self.config, app_settings.validate_settings(overrides))
        app_settings.save_user_settings(app_settings.current_overrides(merged))
        old = self.config
        self.config = merged
        interval = merged['refresh']['interval_minutes']
        if interval != old.get('refresh', {}).get('interval_minutes'):
            self.poll_manager.set_interval(interval * 60 * 1000)
        if merged.get('update') != old.get('update'):
            self._rebuild_tray_menu()
        if 'appearance' in overrides:
            self._reload_background()
        if 'application' in overrides:
            self._apply_identity()

    def _reset_window_size(self):
        """托盘菜单:恢复默认大小(宽度回配置值,面板高度回到随内容)"""
        self.ui_state['window_width'] = None
        self.ui_state['panel_height'] = None
        save_ui_state(self.ui_state)
        self._relayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_grips()

    def _relayout_docked(self):
        """吸附态重排:重算细条几何,尺寸变了才动画过渡(带去重防抖动)

        【为什么要去重】_on_snapshots 每轮刷新都会调 _relayout → 这里。
        无条件重启动画的话,细条会周期性抽搐。短路顺序:
            0. 拖动中 / 窗口未显示 → 不动几何(见下面两段注释)
            1. 动画在播且目标没变 → 让它播完,别打断
            2. 已经到位 → 幂等退出(_docked_collapsed_size 同状态同结果)
            3. 真变了才重播动画
        """
        if self._is_dragging:
            # 拖动细条期间别抢几何:此时窗口被拖离 target 属正常,
            # 若在这里播动画会把窗口从用户手里拽回边缘;
            # 松手时 _maybe_dock / 解吸附逻辑自会定位
            return
        target = self._docked_collapsed_geometry()
        if not self.isVisible():
            # 启动流程(startup_flow 在 show() 前)修剪陈旧账号后 n 变了会走到这;
            # 隐藏窗口播动画无意义,和 _dock_to 的不可见分支一致直接落位
            if self.geometry() != target:
                self.setGeometry(target)
                self._dock_anim_target = QRect(target)
            return
        if (self._dock_geometry_animation.state() == QPropertyAnimation.State.Running
                and self._dock_anim_target == target):
            return
        if self.geometry() == target:
            self._dock_anim_target = QRect(target)
            return
        self._animate_geometry(target)

    # ============================================================
    # PollManager 回调
    # ============================================================
    @Slot(dict)
    def _on_snapshots(self, snapshots: dict):
        """一轮所有账号取完:缓存 + 推给面板 + 重排悬浮条"""
        self._snapshots = dict(snapshots)
        self.detail_panel.set_snapshots(self._snapshots)
        self.detail_panel.clear_refreshing()
        self._relayout()

    def _on_account_refresh(self, account_id: str):
        """看板上某账号的刷新图标:只刷新这一个账号(结果回来前图标变暗)"""
        if self.poll_manager.refresh_account(account_id):
            self.detail_panel.set_account_refreshing(account_id)

    # ============================================================
    # 账号管理(详情面板信号的处理)
    # ============================================================
    def _read_selection_from_panel(self):
        """把详情面板里当前的模式/勾选读回 ui_state"""
        mode = self.detail_panel.get_display_mode()
        self.ui_state['display_mode'] = mode
        if mode == MODE_SINGLE:
            self.ui_state['active_account_id'] = self.detail_panel.get_active_account_id()
        else:
            self.ui_state['visible_account_ids'] = self.detail_panel.get_visible_account_ids()

    def _on_mode_changed(self, mode: str):
        """切换显示模式:只改 display_mode,不从(仍是旧布局的)面板反推选择。

        【为什么不读面板】切换瞬间,面板勾选还是旧模式渲染的(上一次 set_accounts 设的)。
        若此时用新规则去读旧布局,会把 active_account_id / visible_account_ids 互相覆盖,
        导致切回去时之前的选择丢了——而 ui_state 里这两个字段本就各自保留着历史。
        正确做法:直接让 set_accounts 按 ui_state 里目标模式的字段重渲染即可。
        """
        self.ui_state['display_mode'] = mode
        self._sync_panel_accounts()   # 按 ui_state 各自保留的字段重建勾选
        save_ui_state(self.ui_state)
        self._relayout()

    def _on_selection_changed(self):
        self._read_selection_from_panel()
        save_ui_state(self.ui_state)
        self._relayout()

    def _on_add_requested(self):
        """打开"添加账号"弹窗(模态)。验证走后台线程,结果回 _on_verify_done。"""
        dlg = AddAccountDialog(self)
        dlg.verify_clicked.connect(
            lambda pid, lbl, cred, d=dlg: self._verify_and_add(pid, lbl, cred, d))
        dlg.exec()

    def _verify_and_add(self, provider_id: str, label: str, credentials: dict, dialog):
        """后台验证一组凭据(还没落库)"""
        worker = VerifyCredentialsWorker(provider_id, credentials)
        # 默认参数固定捕获,避免闭包延迟绑定
        worker.signals.result_ready.connect(
            lambda _aid, snap, pid=provider_id, lbl=label, cred=credentials, d=dialog:
                self._on_verify_done(pid, lbl, cred, snap, d))
        self._verify_worker = worker   # 持有引用防 GC
        QThreadPool.globalInstance().start(worker)

    def _on_verify_done(self, provider_id, label, credentials, snapshot: QuotaSnapshot, dialog):
        self._verify_worker = None
        if snapshot.success:
            try:
                account = self.account_store.add_account(provider_id, label, credentials)
            except AccountStoreError as e:
                self._sync_panel_accounts()
                self._relayout()
                dialog.set_status(False, str(e))
                return
            logger.info("添加账号成功:%s (%s)", account.id, provider_id)
            self._select_after_add(account.id)
            self._sync_panel_accounts()
            self.poll_manager.refresh_now()
            self._relayout()
            dialog.accept()             # 关闭弹窗
        else:
            # 失败:弹窗留在原地,显示原因,用户可改后重试
            if snapshot.is_transient:
                dialog.set_status(False, "网络异常,请稍后重试")
            elif snapshot.credential_status == "expired":
                dialog.set_status(False, "鉴权失败:Key 无效或已失效")
            else:
                dialog.set_status(False, snapshot.error_message or "验证失败")

    def _select_after_add(self, account_id: str):
        """添加后把新账号纳入当前选择(单源切为它;多源勾上它)"""
        if self.ui_state.get('display_mode') == MODE_SINGLE:
            self.ui_state['active_account_id'] = account_id
        else:
            vis = list(self.ui_state.get('visible_account_ids') or [])
            if account_id not in vis:
                vis.append(account_id)
            self.ui_state['visible_account_ids'] = vis
        save_ui_state(self.ui_state)

    def _on_accounts_reordered(self, account_ids: list):
        """设置页拖拽排序:落库后悬浮条/面板都按新顺序显示"""
        try:
            self.account_store.reorder_accounts(account_ids)
        except AccountStoreError as e:
            QMessageBox.warning(self, "排序未保存", str(e))
        self._sync_panel_accounts()   # 失败时也重建,列表回到存储里的顺序
        self._relayout()

    def _on_rename_account(self, account_id: str, label: str):
        """设置页行内改名"""
        try:
            self.account_store.update_account(account_id, label=label)
        except AccountStoreError as e:
            QMessageBox.warning(self, "改名未保存", str(e))
        self._sync_panel_accounts()
        self._relayout()

    def _on_delete_account(self, account_id: str):
        try:
            self.account_store.delete_account(account_id)
        except AccountStoreError as e:
            self._snapshots.pop(account_id, None)
            self.poll_manager._snapshots.pop(account_id, None)
            self.detail_panel.set_snapshots(self._snapshots)
            self._sync_panel_accounts()
            self._relayout()
            QMessageBox.warning(self, "删除未完成", str(e))
            return
        # 清理 ui_state 残留 + 内存快照
        if self.ui_state.get('active_account_id') == account_id:
            self.ui_state['active_account_id'] = None
        vis = self.ui_state.get('visible_account_ids') or []
        self.ui_state['visible_account_ids'] = [i for i in vis if i != account_id]
        save_ui_state(self.ui_state)
        self._snapshots.pop(account_id, None)
        self.poll_manager._snapshots.pop(account_id, None)
        self._sync_panel_accounts()
        self.detail_panel.set_snapshots(self._snapshots)
        self._relayout()

    def toggle_detail(self):
        """切换详情面板展开/折叠(带动画)

        【为什么用动画】
            突然展开/收起视觉上很跳。用 QPropertyAnimation 在 250ms 内
            平滑改变窗口高度,看起来更舒服。

        【动画原理】
            QPropertyAnimation 监视 QObject 的某个 Qt 属性(这里是 "size"),
            按时间在 startValue 和 endValue 之间插值,
            每帧通过 setter 修改属性,触发重绘。
        """
        # 动画中忽略点击,防止重复触发
        if self._is_animating:
            return

        self._is_expanded = not self._is_expanded
        self._is_animating = True

        base_height = self._current_bar_height()

        if self._is_expanded:
            # ====== 展开 ======
            content_height = self._panel_height()
            target_height = base_height + content_height

            # 临时提高最大高度限制(否则 setSize 会被截断)
            self.setMaximumHeight(target_height)

            # 详情面板紧贴悬浮条下方
            panel_y = base_height
            panel_height = content_height
            self.detail_panel.setGeometry(0, panel_y, self.width(), panel_height)
            self.detail_panel.setVisible(True)
            self.detail_panel.update()

            # 设置动画起点和终点
            start_size = QSize(self.width(), base_height)
            end_size = QSize(self.width(), target_height)
            self._height_animation.setStartValue(start_size)
            self._height_animation.setEndValue(end_size)
            self._height_animation.start()
        else:
            # ====== 折叠 ======
            start_size = self.size()    # 当前大小
            end_size = QSize(self.width(), base_height)
            self._height_animation.setStartValue(start_size)
            self._height_animation.setEndValue(end_size)
            self._height_animation.start()

    def _on_animation_finished(self):
        """动画完成回调"""
        self._is_animating = False

        if not self._is_expanded:
            # 折叠完成后隐藏详情面板(避免占用内存/被点中)
            self.detail_panel.setVisible(False)

        self.update()

    def paintEvent(self, event):
        """重绘事件:画悬浮窗本体

        【两种模式】
            - 折叠态:画一个小圆角矩形 + 内容
            - 展开态:画整个大圆角背景 + 顶部悬浮条内容
        【贴边收起态】没有单独分支:窗口本身缩小成细条,
            内容和普通状态共用同一套绘制,只是尺寸变小、间隔变小。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not self._is_expanded:
            self._draw_float_window(painter)
        else:
            self._draw_expanded_background(painter)
            # 注意:高度必须用"收起态悬浮条高度"而不是窗口当前高度!
            # 展开时窗口高度 = 条高 + 面板高度,若用 self.height(),
            # 文字基线会跟着跑到大窗口中间,导致"字乱跑"
            self._draw_float_window_content(
                painter, self.width(), self._bar_content_height())
            self._draw_toggle_button(painter)

    def _draw_float_window(self, painter: QPainter):
        """绘制悬浮窗(收起态):背景 + 内容
        (用 self.width()/height() 而不是配置值,贴边缩小成细条时背景也跟着缩)

        【背景形状】
            - 正常/弹出状态:圆角矩形(radius = 配置的 corner_radius)
            - 贴边细条:胶囊形(两端是向外凸的半圆弧,像从屏幕边缘长出来)
        """
        width = self.width()
        height = self.height()
        corner_radius = self.config['window']['corner_radius']
        base_width = self._base_width()
        base_height = self.config['window']['height']

        if self._docked and (width != base_width or height != base_height):
            # 贴边细条:胶囊形。半径 = 短边一半(两端整段半圆弧),
            # 但不超过配置半径 —— 多源贴顶条高可到 8+n*20(如 3 账号 68px),
            # 不设上限的话半径 34 会比自由态的 25 滚圆得多,视觉突兀。
            # 弹出动画过渡时短边连续变化(34→50),到完整尺寸时
            # min(w,h)/2 恰好 = 配置半径 25,clamp 不生效,无缝衔接不跳变
            pill_radius = min(min(width, height) / 2, corner_radius)
            self._paint_background(painter, pill_radius)
        else:
            self._paint_background(painter, corner_radius)

        # 贴边细条:内容用细条自身尺寸
        if self._docked:
            # 【为什么按 dock_edge 判断而不是宽高比】多源贴顶账号多时高度会超过宽度
            # (如 10 账号 = 208 高 > 200 宽),按 width<height 会误判成竖条画法;
            # 贴顶变形动画的中间帧(高窗口缩到矮条)同理会闪现竖条。
            # _dock_edge 在 _dock_to 里先于动画赋值,全程稳定。
            if self._dock_edge in ('left', 'right'):
                self._draw_vertical_content(painter, width, height)
            else:
                # 多源贴顶字号不缩(scale_floor=1.0);单源贴顶保持旧版 0.8
                scale_floor = (1.0
                               if self.ui_state.get('display_mode') == MODE_MULTI
                               else 0.8)
                self._draw_float_window_content(painter, width, height, scale_floor)
        else:
            # 正常/折叠动画:内容高度用"收起态悬浮条高度"(单源=基准;多源=动态)
            # 折叠动画时窗口高度缩回条高,若用 self.height(),
            # 文字基线会跟着乱跑(和展开动画同一问题)
            self._draw_float_window_content(
                painter, width, self._bar_content_height())
            self._draw_toggle_button(painter)

    def _draw_vertical_content(self, painter: QPainter, strip_w: int, strip_h: int):
        """竖直细条(贴左/右边缘):每个可见账号一张小卡片,纵向堆叠

        【展示什么 = 顶栏单源展示什么】档位选自 _bar_chips(与 _draw_bar_single
        同源):顶栏横排几个 chip,这里就竖排几行:
            标签       <- 账号名(灰,超宽省略号;不画圆点)
            ● 5h 42%   <- chip 1(圆点配档位状态色 + 白字)
            ● 周 15%   <- chip 2(若有;纯金额账号则是 "● ¥0.00" 一行)
        单源一张卡片垂直居中;多源逐账号堆叠(高度见 _side_blocks 高度计算)。
        """
        # 先把每个可见账号 → (标签, chips),chips 为空按 [(灰, "--")] 兜底
        accounts = {a.id: a for a in self.account_store.list_accounts()}
        ids = self._visible_account_ids()
        cards = []   # [(账号名, chips), ...]
        for i in ids:
            acc = accounts.get(i)
            if not acc:
                continue
            chips = self._bar_chips(self._snapshots.get(i))
            cards.append((acc.label, chips if chips else [(COLOR_TEXT_GRAY, "--")]))

        if not cards:
            # 无可见账号:占位提示,与悬浮条收起态的空态写法一致
            painter.setPen(COLOR_TEXT_GRAY)
            font = QFont(); font.setPointSize(9); painter.setFont(font)
            painter.drawText(0, 0, strip_w, strip_h, Qt.AlignmentFlag.AlignCenter, "--")
            return

        name_h, chip_h = 14, 16   # 名称行 / 每个 chip 行的像素高度

        def card_h(chips) -> int:
            """一张卡片的高度:名称行 + chips 行数"""
            return name_h + len(chips) * chip_h

        def draw_card(top: int, label: str, chips):
            """在 top 起画一张卡片:名称行 + 每 chip 一行,水平居中"""
            pad_x = 6
            # 名称行:只画账号名(8pt 灰),不画圆点 —— 竖条里逐账号都有 chip 行
            # 的状态色圆点了,名称行再带点显得重复
            font = QFont(); font.setPointSize(8); painter.setFont(font)
            fm = painter.fontMetrics()
            label = self._elided_label(fm, label, strip_w - 2 * pad_x)
            x = (strip_w - fm.horizontalAdvance(label)) // 2
            painter.setPen(COLOR_TEXT_SECONDARY)
            painter.drawText(x, top + fm.ascent() + 1, label)
            # chip 行:圆点(档位状态色)+ 档位文字(9pt 白),与顶栏 chip 同构、竖着排
            font = QFont(); font.setPointSize(9); painter.setFont(font)
            fm = painter.fontMetrics()
            for k, (col, text) in enumerate(chips):
                cy = top + name_h + k * chip_h + chip_h // 2
                cdot = 7
                total_w = cdot + 3 + fm.horizontalAdvance(text)
                cx = (strip_w - total_w) // 2
                painter.setPen(col); painter.setBrush(col)
                painter.drawEllipse(cx, cy - cdot // 2, cdot, cdot)
                painter.setPen(COLOR_TEXT_WHITE)
                painter.drawText(cx + cdot + 3, cy + 5, text)

        # 整组卡片垂直居中;高度公式与 _docked_collapsed_size 侧边分支一致
        total_h = sum(card_h(chips) for _label, chips in cards)
        y0 = (strip_h - total_h) // 2
        top = y0
        for label, chips in cards:
            draw_card(top, label, chips)
            top += card_h(chips)

    def _draw_expanded_background(self, painter: QPainter):
        """绘制展开态的完整圆角背景"""
        self._paint_background(painter, self.config['window']['corner_radius'])

    # 背景图预先缩到这个尺寸以内,窗口变形动画时每帧重缩放才不卡
    _BG_MAX_SIDE = 1600

    def _reload_background(self):
        """按 config['appearance'] 重新准备背景色和背景图(图缺失/损坏则只用颜色)"""
        look = self.config.get('appearance', {})
        color = QColor(look.get('bg_color', '#000000'))
        color.setAlpha(round(look.get('bg_opacity', 70) * 255 / 100))
        self._bg_color = color
        path = app_settings.bg_image_path(look.get('bg_image', ''))
        pix = QPixmap.fromImage(QImage(path)) if path else QPixmap()
        if not pix.isNull() and max(pix.width(), pix.height()) > self._BG_MAX_SIDE:
            pix = pix.scaled(self._BG_MAX_SIDE, self._BG_MAX_SIDE,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self._bg_pixmap = None if pix.isNull() else pix
        self._bg_scaled = None          # 按窗口尺寸缓存的缩放结果
        self.update()

    def _paint_background(self, painter: QPainter, radius: float):
        """圆角区域内:背景图(铺满裁剪) + 背景色(有图时作为遮罩)"""
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(0, 0, self.width(), self.height()), radius, radius)
        if self._bg_pixmap is not None:
            dpr = self.devicePixelRatioF()
            target = QSize(round(self.width() * dpr), round(self.height() * dpr))
            if self._bg_scaled is None or self._bg_scaled[0] != target:
                scaled = self._bg_pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                Qt.TransformationMode.SmoothTransformation)
                scaled.setDevicePixelRatio(dpr)
                self._bg_scaled = (target, scaled)
            scaled = self._bg_scaled[1]
            x = (self.width() - scaled.width() / dpr) / 2
            y = (self.height() - scaled.height() / dpr) / 2
            painter.save()
            painter.setClipPath(shape)
            painter.drawPixmap(QPoint(round(x), round(y)), scaled)
            painter.restore()
        painter.fillPath(shape, self._bg_color)

    def _draw_float_window_content(self, painter: QPainter, width: int, height: int,
                                   scale_floor: float = 0.8):
        """绘制悬浮条内容:单源=单账号双 chip(与旧版像素一致);多源=堆叠表格行

        【宽度/高度参数】
            由调用方传入,贴边缩小成细条时传入的是细条尺寸,布局按比例缩小。

        【scale_floor:字号下限】
            默认 0.8(旧行为,单源贴顶 150 宽 → scale=0.8,恰好好看,不动)。
            多源贴顶传 1.0:细条虽窄(200 < 基准 260),但每行高 20px 足够
            放 9pt 字,再缩到 7.2pt 就看不清了 —— 窄靠截断账号名解决,不靠缩字。
        """
        base_width = self._base_width()
        scale = max(scale_floor, width / base_width)

        accounts = {a.id: a for a in self.account_store.list_accounts()}
        ids = self._visible_account_ids()
        rows = [(accounts[i], self._snapshots.get(i)) for i in ids if i in accounts]

        if not rows:
            painter.setPen(COLOR_TEXT_GRAY)
            font = QFont(); font.setPointSize(9 * scale); painter.setFont(font)
            painter.drawText(0, 0, width, height, Qt.AlignmentFlag.AlignCenter,
                             "无账号 · 点齿轮添加")
            return

        # 单源且只有 1 个账号 → 复刻旧版双 chip;否则堆叠行
        if self.ui_state.get('display_mode') == MODE_SINGLE and len(rows) == 1:
            self._draw_bar_single(painter, width, height, scale, rows[0])
        else:
            self._draw_bar_multi(painter, width, height, scale, rows)

    def _bar_chips(self, snapshot: Optional[QuotaSnapshot]):
        """把一个账号的快照浓缩成"该展示的档位"列表 [(圆点色, 文字), ...] 最多 2 个。

        【这是"悬浮窗该展示什么"的唯一权威定义】
            - 顶栏单源(_draw_bar_single)、侧栏卡片(_draw_vertical_content)共用,
              保证同一账号在贴顶/贴侧展示的档位完全一致;
            - 百分比账号:左档优先 5h 窗口,右档优先 周/7d/30d/月(找不到不硬塞);
            - 纯金额账号(DeepSeek 等只有钱包档):显示剩余金额 —— 关键是不依赖
              百分比,只看有没有金额档,所以余额 ¥0.00 也能显示(旧版侧栏用
              _primary_metric 只认能算百分比的档,DeepSeek 没有 used/total
              就全跳过,显示成 "--")。
        """
        tiers = snapshot.tiers if (snapshot and snapshot.success) else []

        percent_tiers = [t for t in tiers
                         if t.kind != KIND_MONEY and t.utilization is not None]
        money_tier = next((t for t in tiers if t.kind == KIND_MONEY), None)

        chips = []
        if percent_tiers:
            left = next((t for t in percent_tiers if t.name == TIER_FIVE_HOUR), None) \
                or percent_tiers[0]
            lp = int(left.utilization)
            chips.append((get_percentage_color(lp), f"{tier_short_label(left)} {lp}%"))
            right = next((t for t in percent_tiers
                          if t.name in (TIER_WEEKLY_LIMIT, TIER_SEVEN_DAY,
                                        TIER_THIRTY_DAY, TIER_MONTHLY)
                          and t is not left), None)
            if right is None:
                right = next((t for t in percent_tiers if t is not left), None)
            if right is not None:
                rp = int(right.utilization)
                chips.append((get_percentage_color(rp),
                              f"{tier_short_label(right)} {rp}%"))
        elif money_tier is not None:
            u = money_tier.effective_utilization()
            col = get_percentage_color(int(u)) if u is not None else COLOR_TEXT_GRAY
            chips.append((col, format_money(money_tier.remaining, money_tier.unit)))
        return chips

    # ====== 悬浮条:单源(双 chip,自动选两个档位)======
    def _draw_bar_single(self, painter: QPainter, width: int, height: int,
                         scale: float, row):
        """单源小条:按账号的额度形态(percent/money)挑最合适的展示。

        【为什么不能"假设左右两个都是百分比档"】旧版固定画左右两个百分比 chip,
        对纯金额账号(DeepSeek 等只有一个钱包档)会硬塞一个假的"7d --%",
        还会把"余额"档当成百分比来显示。选档逻辑在 _bar_chips(与侧栏共用),
        这里只负责把 chips 横排画出来。
        """
        _account, snapshot = row

        font = QFont(); font.setPointSize(10 * scale); painter.setFont(font)
        fm = painter.fontMetrics()
        margin = int(width * 0.10)
        gap = int(width * 0.06)
        dot_size = int(10 * scale)
        cy = height // 2

        chips = self._bar_chips(snapshot)

        if not chips:
            painter.setPen(COLOR_TEXT_GRAY)
            painter.drawText(0, 0, width, height, Qt.AlignmentFlag.AlignCenter, "--")
            return

        def draw_dot_text(color, text, dot_x, text_x):
            """在一个 chip 的位置画圆点 + 紧跟的文字。"""
            painter.setPen(color); painter.setBrush(color)
            painter.drawEllipse(dot_x, cy - dot_size // 2, dot_size, dot_size)
            painter.setPen(COLOR_TEXT_WHITE)
            painter.drawText(text_x, cy + 5, text)

        # 第一个 chip 永远居左
        c0, t0 = chips[0]
        draw_dot_text(c0, t0, margin, margin + dot_size + gap)
        # 第二个 chip(若有)右对齐
        if len(chips) > 1:
            c1, t1 = chips[1]
            rtw = fm.horizontalAdvance(t1)
            rtx = width - margin - rtw
            draw_dot_text(c1, t1, rtx - gap - dot_size, rtx)

    @staticmethod
    def _elided_label(fm, text: str, max_w: int) -> str:
        """按可用像素宽度截断账号名并加省略号,避免硬切字符(旧版 [:6] 把 DeepSeek 砍成 deepse)。"""
        if max_w <= 0:
            return ""
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, max_w)

    # ====== 悬浮条:多源(每个可见账号一行)======
    def _draw_bar_multi(self, painter: QPainter, width: int, height: int,
                        scale: float, rows):
        n = len(rows)
        row_h = height // n
        margin = int(width * 0.08)
        dot = int(9 * scale)
        gap = int(6 * scale)
        for i, (account, snapshot) in enumerate(rows):
            y0 = i * row_h
            cy = y0 + row_h // 2
            pct, _txt = self._primary_metric(snapshot)
            dot_color = get_percentage_color(int(pct)) if pct is not None else COLOR_TEXT_GRAY
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(dot_color)
            painter.drawEllipse(margin, cy - dot // 2, dot, dot)

            font = QFont(); font.setPointSize(9 * scale); painter.setFont(font)
            fm = painter.fontMetrics()
            right = self._row_right_text(snapshot)
            right_x = width - margin - fm.horizontalAdvance(right)
            # 账号名按"到右侧文字之间"的可用宽度省略,不再固定切 6 字符
            label_x = margin + dot + gap
            label = self._elided_label(fm, account.label, right_x - label_x - gap)
            painter.setPen(COLOR_TEXT_WHITE)
            painter.drawText(label_x, cy + 5, label)
            painter.drawText(right_x, cy + 5, right)

    # ====== 指标提取小工具(悬浮条/竖条共用)======
    def _row_right_text(self, snapshot: Optional[QuotaSnapshot]) -> str:
        """账号行右侧紧凑文字(最多前 2 个档位)

        统一走 format_money / format_tier_value,保证与详情列表显示一致(精度、负数钳制)。
        """
        if not snapshot or not snapshot.success or not snapshot.tiers:
            return "--"
        parts = []
        for t in snapshot.tiers[:2]:
            if t.kind == KIND_MONEY:
                parts.append(format_money(t.remaining, t.unit))
            elif t.utilization is not None:
                parts.append(f"{tier_short_label(t)} {format_tier_value(t)}")
        return "  ".join(parts) if parts else "--"

    def _primary_metric(self, snapshot: Optional[QuotaSnapshot]):
        """取"最差档位"的 (用于配色的百分比, 显示文字)"""
        if not snapshot or not snapshot.success:
            return None, "--"
        worst_tier = None
        worst_u = None
        for t in snapshot.tiers:
            u = t.effective_utilization()
            if u is not None and (worst_u is None or u > worst_u):
                worst_u, worst_tier = u, t
        if worst_tier is not None and worst_tier.kind == KIND_MONEY:
            return worst_u, format_money(worst_tier.remaining, worst_tier.unit)
        if worst_tier is not None:
            return worst_u, f"{int(worst_u)}%"
        return None, "--"

    def mousePressEvent(self, event):
        """鼠标按下事件

        【目的】
            记录拖动起点,准备可能开始的拖动。
            不立即拖动,等到鼠标移动距离超过阈值才视为拖动(mouseMoveEvent 里判断),
            这样允许用户单击触发 toggle_detail 而不被误判为拖动。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            # 变形动画还没跑完时按下:立即停在当前形态,防止动画和拖动打架
            self._dock_geometry_animation.stop()
            # event.pos():相对本控件的坐标
            self._click_start_pos = event.pos()
            # event.globalPosition().toPoint():全局屏幕坐标
            # 减去窗口左上角坐标,得到"鼠标相对窗口左上角的偏移量"
            # 拖动时用这个偏移量重新定位窗口,保证鼠标抓在原地
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._is_dragging = False
            event.accept()

    def mouseMoveEvent(self, event):
        """鼠标移动事件(鼠标按下状态下的移动)"""
        if event.buttons() & Qt.MouseButton.LeftButton:
            # 还没进入拖动态时,判断是否超过阈值
            if not self._is_dragging:
                # manhattanLength:曼哈顿距离(|dx|+|dy|),比欧几里得距离算得快
                diff = (event.pos() - self._click_start_pos).manhattanLength()
                if diff > self._click_threshold:
                    self._is_dragging = True
                    # 在真正开始拖动时重新计算抓取偏移
                    # (拖起瞬间窗口可能在贴边位置,用按下时的偏移会抓歪)
                    self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    self.update()

            # 拖动态:用全局坐标减去初始偏移,得到新的窗口左上角坐标
            # 贴边细条拖起时保持细条形态,松手离开边缘后才恢复完整大小
            if self._is_dragging:
                self.move(event.globalPosition().toPoint() - self._drag_position)
                event.accept()
        elif not self._docked and self._toggle_rect().contains(event.pos()):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event):
        """鼠标释放事件

        【点击 vs 拖动的区分】
            - 拖动过   -> 视为拖动结束 -> 贴边或保存位置
            - 没拖动过 -> 视为点击     -> 只有点在底部按钮上才切换展开/收起
        两种情况互斥,用 if/else 比连写两个 if 更清晰。

        【贴边状态】
            悬停/点击都不变形(细条保持细条);只有拖离边缘松手才恢复完整大小。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            if self._docked and not self._is_dragging:
                # 贴边状态下点击细条:不做任何事(不弹出、不展开面板)
                event.accept()
                return

            # 拖动结束就保存位置(立即写盘,兼容 os._exit/关机/强杀)
            # saveGeometry():Qt API,返回窗口几何信息的 QByteArray
            if self._is_dragging:
                # 拖到屏幕边缘松手 -> 贴边收起;否则保持自由位置。
                # 展开态不贴边:展开/收起只由底部按钮控制,拖动不改变展开状态
                edge = None if self._is_expanded else self._maybe_dock()
                if edge:
                    self._dock_to(edge)
                elif not self._docked:
                    # 自由态拖动:只保存位置,尺寸不动(展开态保持展开)
                    WindowManager.save_geometry(self.saveGeometry(), dock_edge=None)
                else:
                    # 拖离边缘:先记住细条形态,恢复最小高度限制后再平滑变回完整大小
                    strip_geometry = self.geometry()
                    self._undock()
                    target = QRect(strip_geometry.topLeft(),
                                   QSize(self._base_width(),
                                         self._current_bar_height()))
                    # 统一走 _animate_geometry,保持 _dock_anim_target
                    # 与实际动画目标一致(这里虽已解除吸附,去重逻辑靠它比对)
                    self._animate_geometry(target)
                    # 清除贴边状态,下次启动不恢复吸附
                    WindowManager.save_geometry(self.saveGeometry(), dock_edge=None)
            elif self._toggle_rect().contains(event.pos()):
                # 没拖动过、且点在底部展开/收起按钮上才切换;点面板其它地方不响应
                self.toggle_detail()

            self._is_dragging = False
            event.accept()

    def _on_autostart_toggled(self, enable: bool):
        """开机自启开关变化:立即写入注册表"""
        success = autostart.set_enabled(enable)
        if not success and enable:
            # 开启失败时回退复选框,保持 UI 和实际状态一致
            self.detail_panel.set_autostart_checked(False)

    def _on_close_service(self):
        """处理关闭服务:无论何种情况都强制退出进程

        【为什么用 os._exit(0) 而不是常规关闭】
            早期用 self.close() / QApplication.quit() / sys.exit() 时,
            点"关闭服务"后进程常常仍残留在后台(任务管理器里能看到),
            不能可靠退出。诱因通常是系统托盘(QSystemTrayIcon)持有引用、
            以及 Qt 事件循环在这些路径下不一定干净结束;具体根因没有
            完全定位,但 os._exit(0) 直接终止进程,能确定性地保证退出。

            代价:os._exit 绕过 Python 所有退出清理(atexit/__del__/finally)
            和 Qt 的 closeEvent。所以本程序不依赖退出回调来保存状态——
            窗口位置/贴边状态在拖动结束时(mouseReleaseEvent)就立即写盘,
            强杀也不会丢。
        """
        # 停止轮询(停定时器 + 清空等待中的 worker),避免退出过程中再发请求
        try:
            self.poll_manager.stop()
        except Exception as e:
            logger.debug("停止轮询失败(可忽略): %s", e)

        # provider Session 由每次采集的 worker 在 finally 中关闭。

        # 隐藏托盘图标,避免退出后残留"僵尸图标"
        try:
            self.tray_icon.hide()
        except Exception as e:
            logger.debug("隐藏托盘失败(可忽略): %s", e)

        # 强制终止进程:绕过 Python 退出清理
        os._exit(0)

    # ============================================================
    # 自动更新:检查 / 弹窗 / 下载 / 替换重启
    # ============================================================
    def _check_for_updates(self, manual: bool = False):
        """发起后台更新检查(manual=True 来自托盘菜单,失败要给用户反馈)。

        worker 挂到全局线程池;引用存到 self 上防被 GC(模式同 _verify_and_add)。
        检查进行中时忽略再次触发(防"快速点两次托盘菜单弹两个窗")。
        """
        if not self.config.get('update', {}).get('enabled', False):
            return
        if getattr(self, '_update_check_worker', None) is not None:
            return
        worker = UpdateCheckWorker()
        worker.signals.checked.connect(
            lambda result: self._on_check_done(result, manual))
        self._update_check_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_check_done(self, result, manual: bool):
        """检查结果回来(主线程):决定弹什么、或什么都不弹。

        - result 为 None:无新版或检查失败。手动检查时告知一声,自动检查静默。
        - 有新版但用户选过"此版本不再提醒":静默跳过(手动检查也尊重,免弹窗骚扰)。
        - 其余情况弹 UpdateDialog,按 exec() 返回后的结果行动。
        """
        self._update_check_worker = None
        if result is None:
            if manual:
                QMessageBox.information(
                    self, "检查更新",
                    "已是最新版本,或暂时无法获取版本信息。\n"
                    "(网络异常时请稍后再试)")
            return

        if self.ui_state.get('skip_update_version') == result['tag']:
            logger.info("更新:版本 %s 已被用户标记跳过", result['tag'])
            return

        dlg = UpdateDialog(APP_VERSION, result['tag'], result['notes'], self)
        dlg.exec()                        # 模态:在这里等用户做完决定
        if dlg.update_accepted():
            self._on_update_confirmed(result)
        elif dlg.skip_checked():
            # "本次跳过"且勾选了不再提醒 → 记住这个 tag,下次静默
            self.ui_state['skip_update_version'] = result['tag']
            save_ui_state(self.ui_state)

    def _on_update_confirmed(self, result: dict):
        """用户点了「立即更新」:硬门禁后起下载 worker,弹进度框。"""
        if not self.config.get('update', {}).get('enabled', False):
            return
        if not updater.replace_allowed():
            # 开发环境只演示到这里:替换目录会毁掉仓库,绝不能真跑
            QMessageBox.information(
                self, "开发环境",
                "开发环境仅演示更新流程,不执行目录替换。\n"
                "完整更新流程只在打包后的正式版里生效。")
            return

        self._update_download_worker = UpdateDownloadWorker(
            result['asset_url'], result['zip_name'], result['sha256'])
        w = self._update_download_worker

        self._update_progress = UpdateProgressDialog(
            on_cancel=w.cancel, parent=self)
        self._update_progress.show()

        w.signals.progress.connect(self._update_progress.on_progress)
        w.signals.succeeded.connect(self._on_update_ready)
        w.signals.failed.connect(self._on_update_failed)
        QThreadPool.globalInstance().start(w)

    def _on_update_ready(self, staging_dir: str):
        """下载+校验+解压都完成:生成 ps1、脱离式启动,【确认在跑】才退出让位。

        退出前必须确认更新脚本真的执行起来了(launch_updater 会等 update.log
        出现)——否则被安全软件静默拦截时,程序退出、脚本没跑,用户下次双击
        exe 仍是旧版,还把 staging/zip 当残留清了(v1.0.2 的真实事故)。
        """
        try:
            ps1 = updater.write_update_ps1(staging_dir, os.getpid())
            if not updater.launch_updater(ps1):
                self._on_update_failed(
                    "更新脚本启动失败(可能被安全软件拦截),请手动下载更新")
                return
            self._update_progress.close()
            # os._exit 不经过 logging 的收尾刷盘,先手动刷,
            # 否则刚才"更新脚本已启动"这条日志可能丢
            logging.shutdown()
            # 复用既有的强退链路(停轮询/关 session/藏托盘);
            # ps1 会等本进程消失再动手替换
            self._on_close_service()
        except Exception as e:
            logger.exception("启动更新失败")
            self._on_update_failed(f"启动更新失败:{e}")

    def _on_update_failed(self, msg: str):
        """更新链路失败:提示用户并清半成品,程序继续正常运行。

        用户主动取消(msg 为"已取消")不算错误:静默关进度框,不弹警告。
        """
        logger.warning("更新失败: %s", msg)
        try:
            self._update_progress.close()
        except Exception:
            pass
        # 下载了一半的 zip 是坏档,删掉免得占 50MB;staging 结构完整,
        # 留着没有意义,一并清(下次更新会重建)
        updater.cleanup_leftovers()
        if msg == "已取消":
            return
        QMessageBox.warning(self, "更新未完成", msg + "\n程序将继续使用当前版本。")

    def startup_flow(self):
        """启动流程(public,main.py 在 show() 前调用)

        【策略】
            - 启动轮询(会立即取一次,结果经 _on_snapshots 回来)
            - 有启用账号 -> 收起态显示悬浮条
            - 无账号 -> 展开账号管理页,引导添加
        """
        # 1) 修正 ui_state 里指向"已不存在账号"的陈旧选择
        enabled = self._enabled_account_ids()
        active = self.ui_state.get('active_account_id')
        if active not in enabled:
            self.ui_state['active_account_id'] = enabled[0] if enabled else None
        self.ui_state['visible_account_ids'] = [
            i for i in (self.ui_state.get('visible_account_ids') or []) if i in enabled
        ]
        save_ui_state(self.ui_state)
        self._sync_panel_accounts()

        # 2) 启动轮询(首次立即取;之后按 config.refresh.interval_minutes 定时取)
        self.poll_manager.start()
        self._relayout()
        # show 之后 Qt 可能再次套用 restoreGeometry 恢复的旧高度,盖掉上面的 resize;
        # 排一个 0ms 延迟,在 show 完成后(事件循环第一拍)再 _relayout 一次,
        # 把收起态高度强制校准回当前模式应有的值(治"启动很矮、展开后才正常")
        QTimer.singleShot(0, self._relayout)

        # 3) 更新检查:先清掉上次更新的残留(staging/半成品 zip),再按配置
        #    延迟 5 秒静默查一次 Gitee(错开首轮额度轮询,失败只写日志)
        updater.cleanup_leftovers()
        if (self.config.get('update', {}).get('enabled', False)
                and self.config.get('update', {}).get('check_on_startup', False)
                and updater.update_supported()):
            QTimer.singleShot(5000, lambda: self._check_for_updates(manual=False))

        # 4) 有账号:收起态即可;无账号:展开账号管理引导添加
        if enabled:
            return

        if self._docked:
            self._undock()

        # 先标记展开,再进设置页(toggle_settings 会发 relayout_requested,
        # _relayout 据此按"展开态"算窗口高度 = 条高 + 面板高,无需手动 size)
        self._is_expanded = True
        self.detail_panel.setVisible(True)
        self.detail_panel.toggle_settings()
        self._relayout()
