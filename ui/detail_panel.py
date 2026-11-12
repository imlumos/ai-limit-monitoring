"""
详情面板 - 账号列表(绘制)+ 设置(控件)+ 添加账号弹窗
========================================================

【两个页面】(右上角齿轮进入设置,关闭图标退出设置)
    1. 账号列表页:每个账号一块,显示各档位进度条/余额
    2. 设置页:显示模式(单源/多源)、账号列表(勾选可见性)、添加/删除、开机自启、关闭

【添加账号】单独弹窗(AddAccountDialog)。平台一换,凭据输入框按该平台的
credential_schema 自动重建(GLM 1 个、GLM 团队 3 个…)。

【高度策略(无滚动条)】
    面板高度随内容自适应:账号多/档位多就变高,少就变矮。内容变化时发
    relayout_requested 信号,主窗口据此重排窗口尺寸(展开=条高+面板高)。
    不用滚动条,放不下就整体长高。

【和主窗口通信】(只发信号,业务在主窗口)
    add_requested()            打开"添加账号"弹窗
    delete_account_clicked(id) 删除账号
    mode_changed(mode)         显示模式切换
    selection_changed()        可见性勾选变化
    relayout_requested()       面板内容高度变了,请主窗口重排
    autostart_toggled(checked) / close_clicked()
主窗口再把账号列表/快照推回来:set_accounts / set_snapshots。
"""
import logging
from datetime import datetime
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                                 QLabel, QLineEdit, QPushButton, QComboBox,
                                 QListWidget, QListWidgetItem, QRadioButton,
                                 QButtonGroup, QCheckBox, QDialog,
                                 QStyledItemDelegate, QAbstractItemView, QToolTip)
from PySide6.QtCore import Qt, Signal, QTimer, QPoint, QEvent
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QPainterPath
from typing import Optional, List, Dict

from api.models import (QuotaSnapshot, QuotaTier, KIND_MONEY,
                        tier_display_name, format_money)
from api.registry import list_providers, get_provider
from ui.colors import (get_percentage_color, COLOR_TEXT_WHITE, COLOR_TEXT_GRAY,
                        COLOR_TEXT_SECONDARY, COLOR_PROGRESS_BAR_BG)

logger = logging.getLogger(__name__)

_INPUT_QSS = ("QLineEdit{background:#2a2a2a;color:white;border:1px solid #555;"
              "border-radius:3px;padding:4px 6px;}")


_LABEL_ROLE = Qt.ItemDataRole.UserRole + 1   # 列表项里存账号名(显示文本带平台后缀,不能直接编辑)
_LABEL_MAX_LEN = 20


class _LabelEditDelegate(QStyledItemDelegate):
    """双击行内改名:编辑框只给账号名;提交不改列表项,交给 on_commit(id, 新名) 落库后重建列表。"""
    def __init__(self, on_commit, parent=None):
        super().__init__(parent)
        self._on_commit = on_commit

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setMaxLength(_LABEL_MAX_LEN)
        # 不用 _INPUT_QSS 的上下内边距:行高只有 ~26px,内边距会把文字上下切掉
        editor.setStyleSheet("QLineEdit{background:#2a2a2a;color:white;border:1px solid #4CAF50;"
                             "border-radius:3px;padding:0 4px;font-size:10px;}")
        return editor

    def updateEditorGeometry(self, editor, option, index):
        # 默认几何是扣掉列表项 padding 后的文字区,太矮;横向沿用默认(勾选框右侧),纵向撑满整行
        super().updateEditorGeometry(editor, option, index)
        r = option.rect
        x = editor.geometry().x()
        editor.setGeometry(x, r.y() + 2, r.right() - x - 3, r.height() - 4)

    def setEditorData(self, editor, index):
        editor.setText(index.data(_LABEL_ROLE) or "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        new = editor.text().strip()
        if new and new != index.data(_LABEL_ROLE):
            account_id = index.data(Qt.ItemDataRole.UserRole)
            # 延后到编辑器关闭之后再落库/重建列表,避免在委托回调里 clear() 列表
            QTimer.singleShot(0, lambda: self._on_commit(account_id, new))


class AddAccountDialog(QDialog):
    """添加账号弹窗:平台下拉 + 动态凭据 + 验证并保存。

    验证走后台线程:点"验证并保存"发 verify_clicked(provider_id,label,creds),
    主窗口拿到结果后调 set_status();成功后主窗口调 accept() 关窗。
    """
    verify_clicked = Signal(str, str, dict)  # provider_id, label, credentials

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加账号")
        self.setModal(True)
        self.resize(360, 260)
        self.setStyleSheet(
            "QDialog{background:#222;color:white;} QLabel{color:#ddd;}"
            "QComboBox{background:#2a2a2a;color:white;border:1px solid #555;border-radius:3px;padding:3px;}"
            "QComboBox QAbstractItemView{background:#2a2a2a;color:white;selection-background-color:#4CAF50;}"
            + _INPUT_QSS)

        form = QFormLayout(self)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(10)

        self.provider_combo = QComboBox()
        for p in list_providers():
            self.provider_combo.addItem(p["display_name"], p["provider_id"])
        self.provider_combo.currentIndexChanged.connect(self._rebuild_fields)
        form.addRow("平台:", self.provider_combo)

        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText("如:工作号(可留空)")
        form.addRow("名称:", self.label_input)

        self.cred_container = QWidget()
        self.cred_layout = QVBoxLayout(self.cred_container)
        self.cred_layout.setContentsMargins(0, 0, 0, 0)
        self.cred_layout.setSpacing(6)
        form.addRow(self.cred_container)
        self._cred_inputs: Dict[str, QLineEdit] = {}

        self.verify_btn = QPushButton("验证并保存")
        self.verify_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.verify_btn.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;border:none;border-radius:4px;padding:6px 16px;}"
            "QPushButton:hover{background:#45A049;} QPushButton:disabled{background:#555;}")
        self.verify_btn.clicked.connect(self._on_verify)
        btn_row = QHBoxLayout()
        btn_row.addStretch(); btn_row.addWidget(self.verify_btn); btn_row.addStretch()
        form.addRow(btn_row)

        self.status_lbl = QLabel(" ")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(self.status_lbl)

        self._rebuild_fields()

    def _rebuild_fields(self):
        while self.cred_layout.count():
            it = self.cred_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._cred_inputs = {}
        pid = self.provider_combo.currentData()
        try:
            schema = get_provider(pid).credential_schema
        except Exception:
            schema = []
        for f in schema:
            edit = QLineEdit()
            edit.setPlaceholderText(f.placeholder or "")
            if f.secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(f.label + ":")
            lbl.setMinimumWidth(60)
            row.addWidget(lbl)
            row.addWidget(edit, 1)
            wrap = QWidget()
            wrap.setLayout(row)
            self.cred_layout.addWidget(wrap)
            self._cred_inputs[f.key] = edit
        logger.debug("添加弹窗:平台=%s 重建 %d 个凭据字段", pid, len(self._cred_inputs))

    def _on_verify(self):
        pid = self.provider_combo.currentData() or ""
        label = self.label_input.text().strip() or pid
        creds = {k: e.text().strip() for k, e in self._cred_inputs.items()}
        try:
            schema = get_provider(pid).credential_schema
        except Exception:
            schema = []
        for f in schema:
            if f.required and not creds.get(f.key):
                self.set_status(False, f"请填写{f.label}")
                return
        self.verify_btn.setEnabled(False)
        self.set_status(None, "验证中…")
        logger.info("添加账号:发起验证 provider=%s", pid)
        self.verify_clicked.emit(pid, label, creds)

    def set_status(self, ok: Optional[bool], message: str):
        self.verify_btn.setEnabled(True)
        color = "#FFC107" if ok is None else ("#4CAF50" if ok else "#F44354")
        self.status_lbl.setStyleSheet(f"QLabel{{color:{color};}}")
        self.status_lbl.setText(message)


class DetailPanel(QWidget):
    """详情面板:账号列表页(绘制)+ 设置页(控件)"""

    # ====== 信号 ======
    add_requested = Signal()
    delete_account_clicked = Signal(str)
    mode_changed = Signal(str)
    selection_changed = Signal()
    relayout_requested = Signal()   # 面板高度可能变了,请主窗口重排
    autostart_toggled = Signal(bool)
    close_clicked = Signal()
    accounts_reordered = Signal(list)     # 拖拽排序后的账号 id 顺序
    rename_requested = Signal(str, str)   # account_id, 新名字
    advanced_settings_clicked = Signal()
    account_refresh_clicked = Signal(str)   # 手动刷新单个账号用量(account_id)

    # ====== 账号列表页布局常量 ======
    TOP_PADDING = 36   # 顶部留一行给刷新/齿轮按钮,不和第一个账号标题叠
    ACCOUNT_HEADER_H = 26
    # 档位行内部三层:名称(0~16) + 进度条(20~28) + 重置时间(31~45)。
    # 曾经是 40,第三行"重置 xx:xx"会溢出到下一档,和"每周/每月"叠字,所以取 48
    TIER_H = 48
    ACCOUNT_GAP = 14
    BOTTOM_PADDING = 18
    PANEL_MIN_HEIGHT = 170
    # 设置页高度(确定性公式,不依赖 Qt sizeHint):固定部分 + 账号数×行高
    SETTINGS_FIXED_H = 248   # 模式区 + 列表标题 + 操作提示 + 增删按钮 + 底部(自启/关闭)
    SETTINGS_ROW_H = 30      # 账号列表每行高度

    GEAR_SIZE = 20
    GEAR_MARGIN = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accounts = []
        self._snapshots: Dict[str, QuotaSnapshot] = {}
        self._display_mode = "single"
        self._show_settings = False
        self._gear_rect = None
        self._refreshing_ids = set()   # 手动刷新进行中的账号:图标转圈、不可再点
        self._spin_angle = 0           # 转圈动画当前角度(度)
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(30)
        self._spin_timer.timeout.connect(self._advance_spin)
        self._acc_refresh_rects = {}   # account_id -> 刷新图标矩形(内容坐标,未减滚动偏移)
        self._scroll = 0           # 列表页滚动偏移(用户把面板拖得比内容矮时才会 >0)
        self._drag = None          # 看板拖拽排序状态:{id, start_y, active, insert}
        self.setMouseTracking(True)   # 悬停账号块时显示"可拖动"手形光标
        # 账号列表页内容总高(随账号/档位数变化)
        self._content_height = self.PANEL_MIN_HEIGHT

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._create_settings_widgets()

    # ============================================================
    # 设置页控件
    # ============================================================
    def _create_settings_widgets(self):
        self.settings_widget = QWidget(self)
        self.settings_widget.hide()
        lay = QVBoxLayout(self.settings_widget)
        lay.setContentsMargins(18, 40, 18, 16)
        lay.setSpacing(12)

        # 显示模式
        mode_title = QLabel("显示模式")
        mode_title.setStyleSheet("color:#e0e0e0; font-size:11px; font-weight:bold;")
        # "高级设置 ›" 做成标题行右侧的文字链接:底部按钮行在默认宽度下已放不下第三个控件
        self.advanced_btn = QPushButton("高级设置 ›")
        self.advanced_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_btn.setFlat(True)
        self.advanced_btn.setStyleSheet(
            "QPushButton{color:#8fd19e;background:transparent;border:none;font-size:10px;padding:0;}"
            "QPushButton:hover{color:#b5e8c0;}")
        self.advanced_btn.clicked.connect(self.advanced_settings_clicked.emit)
        title_row = QHBoxLayout()
        title_row.addWidget(mode_title)
        title_row.addStretch()
        title_row.addWidget(self.advanced_btn)
        lay.addLayout(title_row)
        mode_row = QHBoxLayout()
        self.rb_single = QRadioButton("单源聚焦")
        self.rb_multi = QRadioButton("多源同屏")
        for rb in (self.rb_single, self.rb_multi):
            rb.setStyleSheet("QRadioButton{color:#e0e0e0; font-size:10px;}")
        self.rb_single.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.rb_single, 0)
        self._mode_group.addButton(self.rb_multi, 1)
        self._mode_group.idClicked.connect(
            lambda i: self.mode_changed.emit("single" if i == 0 else "multi"))
        mode_row.addWidget(self.rb_single)
        mode_row.addWidget(self.rb_multi)
        lay.addLayout(mode_row)

        # 账号列表(标题文字会随模式在 set_accounts 里切换)
        self.list_title = QLabel("账号(勾选要在悬浮条显示的)")
        self.list_title.setStyleSheet("color:#e0e0e0; font-size:11px; font-weight:bold;")
        lay.addWidget(self.list_title)
        self.account_list = QListWidget()
        self.account_list.setStyleSheet(
            "QListWidget{background:#1f1f1f;color:white;border:1px solid #3a3a3a;"
            "border-radius:4px;font-size:10px;} QListWidget::item{padding:6px 6px;}")
        self.account_list.itemChanged.connect(self._on_item_changed)
        # 窗口可拖窄:不出横向滚动条,过长的"名字 · 平台"末尾省略
        self.account_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.account_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        # 拖拽排序 + 双击/F2 改名
        self.account_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.account_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.account_list.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                          | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.account_list.setItemDelegate(
            _LabelEditDelegate(self.rename_requested.emit, self.account_list))
        self.account_list.model().rowsMoved.connect(
            lambda *_: QTimer.singleShot(0, self._emit_order))
        lay.addWidget(self.account_list)
        hint = QLabel("拖动调整顺序 · 双击修改名称")
        hint.setStyleSheet("color:#8a8a8a; font-size:9px;")
        lay.addWidget(hint)

        # 增 / 删
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("＋ 添加账号")
        self.del_btn = QPushButton("删除选中")
        for b in (self.add_btn, self.del_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:#333;color:white;border:none;border-radius:4px;"
                "padding:6px 10px;font-size:10px;} QPushButton:hover{background:#444;}")
        self.add_btn.clicked.connect(self.add_requested.emit)
        self.del_btn.clicked.connect(self._on_delete_clicked)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.del_btn)
        lay.addLayout(btn_row)

        # 底部:自启 + 关闭
        self.autostart_checkbox = QCheckBox("开机自启动")
        self.autostart_checkbox.setStyleSheet(self._checkbox_qss())
        self.autostart_checkbox.toggled.connect(
            lambda checked: self.autostart_toggled.emit(checked))

        self.close_btn = QPushButton("关闭服务")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFixedWidth(90)
        self.close_btn.setStyleSheet(
            "QPushButton{background:#646464;color:white;border:none;border-radius:5px;"
            "padding:6px 14px;} QPushButton:hover{background:#757575;}")
        self.close_btn.clicked.connect(self.close_clicked.emit)

        bottom = QHBoxLayout()
        bottom.addWidget(self.autostart_checkbox)
        bottom.addStretch()
        bottom.addWidget(self.close_btn)
        lay.addLayout(bottom)

    @staticmethod
    def _checkbox_qss() -> str:
        return (
            "QCheckBox{color:#cfcfcf;font-size:10px;}"
            "QCheckBox::indicator{width:15px;height:15px;border:1px solid #969696;"
            "border-radius:2px;background:#282828;}"
            "QCheckBox::indicator:checked{background:#4CAF50;border:1px solid #4CAF50;}")

    def _on_delete_clicked(self):
        item = self.account_list.currentItem()
        if item is None:
            return
        account_id = item.data(Qt.ItemDataRole.UserRole)
        if account_id:
            logger.info("请求删除账号 %s", account_id)
            self.delete_account_clicked.emit(account_id)

    def _emit_order(self):
        ids = [self.account_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self.account_list.count())]
        self.accounts_reordered.emit(ids)

    def _on_item_changed(self, item: QListWidgetItem):
        """勾选变化:单源模式只保留这一个勾;然后通知主窗口"""
        if self._display_mode == "single" and item.checkState() == Qt.CheckState.Checked:
            self.account_list.blockSignals(True)
            for i in range(self.account_list.count()):
                it = self.account_list.item(i)
                if it is not item:
                    it.setCheckState(Qt.CheckState.Unchecked)
            self.account_list.blockSignals(False)
        self.selection_changed.emit()

    # ============================================================
    # 主窗口推数据进来
    # ============================================================
    def set_accounts(self, accounts, ui_state: dict):
        self._accounts = list(accounts)
        mode = ui_state.get("display_mode", "single")
        self._display_mode = mode
        self._mode_group.blockSignals(True)
        (self.rb_single if mode == "single" else self.rb_multi).setChecked(True)
        self._mode_group.blockSignals(False)
        # 标题随模式变:单源=单选语义,多源=多选语义(治"下拉一直显示多源")
        self.list_title.setText(
            "选择一个要展示的账号" if mode == "single"
            else "账号(勾选要在悬浮条显示的)")

        active_id = ui_state.get("active_account_id")
        visible = set(ui_state.get("visible_account_ids") or [])
        # 空字段兜底,与浮窗 _visible_account_ids 保持一致(避免面板全不勾、浮窗却显示第一个)
        all_ids = [a.id for a in self._accounts]
        if mode == "single":
            if not active_id or active_id not in all_ids:
                active_id = all_ids[0] if all_ids else None
        else:
            if not visible:
                visible = set(all_ids)

        self.account_list.blockSignals(True)
        self.account_list.clear()
        for acc in self._accounts:
            try:
                disp = get_provider(acc.provider_id).display_name
            except Exception:
                disp = acc.provider_id
            state = "（已停用）" if not acc.enabled else ""
            it = QListWidgetItem(f"{acc.label}  ·  {disp}{state}")
            it.setData(Qt.ItemDataRole.UserRole, acc.id)
            it.setData(_LABEL_ROLE, acc.label)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled)
            if mode == "single":
                it.setCheckState(Qt.CheckState.Checked if acc.id == active_id
                                 else Qt.CheckState.Unchecked)
            else:
                it.setCheckState(Qt.CheckState.Checked if acc.id in visible
                                 else Qt.CheckState.Unchecked)
            self.account_list.addItem(it)
        self.account_list.blockSignals(False)
        # 让列表高度容纳全部条目(不出现内部滚动条,整体随面板长高)
        self.account_list.setMinimumHeight(
            max(40, len(self._accounts) * self.SETTINGS_ROW_H + 8))
        logger.debug("面板账号列表刷新:%d 个,模式=%s", len(self._accounts), mode)
        self.update()

    def set_display_mode(self, mode: str):
        self._display_mode = mode

    def set_snapshots(self, snapshots: Dict[str, QuotaSnapshot]):
        self._snapshots = dict(snapshots)
        self.update()

    def set_account_refreshing(self, account_id: str):
        self._refreshing_ids.add(account_id)
        if not self._spin_timer.isActive():
            self._spin_timer.start()
        self.update()

    def clear_refreshing(self):
        self._refreshing_ids.clear()
        self._spin_timer.stop()
        self.update()

    def _advance_spin(self):
        self._spin_angle = (self._spin_angle + 12) % 360   # 30ms × 12° ≈ 0.9 秒一圈
        self.update()

    @staticmethod
    def _data_time_text(snapshot: Optional[QuotaSnapshot]) -> str:
        """账号数据的获取时间(限流/断网保留旧值时就是旧值的时间);非今天带日期"""
        if not snapshot or not snapshot.queried_at:
            return ""
        t = datetime.fromtimestamp(snapshot.queried_at / 1000)
        return t.strftime("%H:%M" if t.date() == datetime.now().date() else "%m-%d %H:%M")

    def set_autostart_checked(self, checked: bool):
        self.autostart_checkbox.blockSignals(True)
        self.autostart_checkbox.setChecked(checked)
        self.autostart_checkbox.blockSignals(False)

    def get_display_mode(self) -> str:
        return "single" if self.rb_single.isChecked() else "multi"

    def get_active_account_id(self) -> Optional[str]:
        for i in range(self.account_list.count()):
            it = self.account_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                return it.data(Qt.ItemDataRole.UserRole)
        return None

    def get_visible_account_ids(self) -> List[str]:
        ids = []
        for i in range(self.account_list.count()):
            it = self.account_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        return ids

    def _displayed_accounts(self):
        """账号列表页要画的账号:按当前选择过滤(与浮窗 _visible_account_ids 同语义)。

        单源=只画选中的那一个(没选中则退到第一个);多源=画所有勾选的(全不勾则全画)。
        选择读 account_list 的勾选状态——它是选择的唯一真实来源,用户勾选即时反映。
        """
        checked = self.get_visible_account_ids()
        by_id = {a.id: a for a in self._accounts}
        if checked:
            return [by_id[i] for i in checked if i in by_id]
        mode = getattr(self, "_display_mode", "single")
        if mode == "single":
            return [self._accounts[0]] if self._accounts else []
        return list(self._accounts)

    # ============================================================
    # 几何 / 高度(无滚动条,内容自适应)
    # ============================================================
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'settings_widget'):
            self.settings_widget.setGeometry(0, 0, self.width(), self.height())

    def _block_spans(self):
        """列表页各账号块的纵向范围 [(account, top, bottom)](内容坐标,未减滚动偏移)"""
        spans, y = [], self.TOP_PADDING
        for acc in self._displayed_accounts():
            snap = self._snapshots.get(acc.id)
            tiers = max(1, len(snap.tiers) if (snap and snap.success) else 1)
            h = self.ACCOUNT_HEADER_H + tiers * self.TIER_H + self.ACCOUNT_GAP
            spans.append((acc, y, y + h))
            y += h
        return spans

    def _recompute_content_height(self):
        """账号列表页内容总高 = 上下留白 + 各账号块(标题+档位)"""
        spans = self._block_spans()
        bottom = spans[-1][2] if spans else self.TOP_PADDING
        self._content_height = bottom + self.BOTTOM_PADDING

    @property
    def showing_settings(self) -> bool:
        return self._show_settings

    def _max_scroll(self) -> int:
        return max(0, self._content_height - self.height())

    def wheelEvent(self, event):
        """列表页内容比面板高时滚轮滚动(内容放得下时不处理)"""
        if self._show_settings or self._max_scroll() == 0:
            event.ignore()
            return
        step = event.angleDelta().y() / 120 * 40
        self._scroll = int(min(max(self._scroll - step, 0), self._max_scroll()))
        self.update()
        event.accept()

    def get_required_height(self) -> int:
        """面板当前需要的高度(无滚动条、无上限,内容多了整体长高):
        设置页 = 固定部分 + 账号数×行高;列表页 = 内容高。"""
        if self._show_settings:
            n = max(1, len(self._accounts))
            return max(self.PANEL_MIN_HEIGHT, self.SETTINGS_FIXED_H + n * self.SETTINGS_ROW_H)
        self._recompute_content_height()
        return max(self.PANEL_MIN_HEIGHT, self._content_height)

    def toggle_settings(self):
        """切换设置页;页面变了高度可能变,通知主窗口重排"""
        self._show_settings = not self._show_settings
        self.settings_widget.setVisible(self._show_settings)
        QToolTip.hideText()
        self.update()
        self.relayout_requested.emit()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and self._hit(self._gear_rect, event.pos()):
            QToolTip.showText(event.globalPos(),
                              "退出设置" if self._show_settings else "打开设置", self)
            return True
        return super().event(event)

    # ============================================================
    # 绘制(账号列表页)
    # ============================================================
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_gear_button(painter)
        self._acc_refresh_rects = {}
        if self._show_settings:
            return

        self._recompute_content_height()

        if not self._accounts:
            painter.setPen(COLOR_TEXT_SECONDARY)
            f = QFont(); f.setPointSize(10); painter.setFont(f)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "点右上角齿轮 → 添加账号")
            return

        # 面板被用户拖矮时内容可滚动:顶部按钮行不动,内容区裁剪后整体上移
        self._scroll = min(self._scroll, self._max_scroll())
        content_top = self.TOP_PADDING - 6
        painter.save()
        painter.setClipRect(0, content_top, self.width(), self.height() - content_top)
        painter.translate(0, -self._scroll)
        y = self.TOP_PADDING
        for acc in self._displayed_accounts():
            snap = self._snapshots.get(acc.id)
            y = self._draw_account_block(painter, acc, snap, y)
        self._draw_drag_feedback(painter)
        painter.restore()
        self._draw_scrollbar(painter, content_top)

    def _draw_account_block(self, painter, account, snapshot: Optional[QuotaSnapshot], y: int) -> int:
        width = self.width()
        if snapshot and snapshot.success:
            worst = snapshot.worst_utilization()
            dot_color = get_percentage_color(int(worst)) if worst is not None else COLOR_TEXT_GRAY
        else:
            dot_color = COLOR_TEXT_GRAY
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot_color)
        painter.drawEllipse(18, y + 8, 10, 10)

        painter.setPen(COLOR_TEXT_WHITE)
        f = QFont(); f.setPointSize(10); f.setBold(True); painter.setFont(f)
        title = account.label   # 看板只显示账号名;平台名在设置页列表里看

        # 标题行最右:数据时间 + 单账号刷新图标;right_edge = 备注/标题可用的右边界
        icon_s = self.ACC_REFRESH_SIZE
        icon_x = width - 18 - icon_s
        icon_y = y + (self.ACCOUNT_HEADER_H - icon_s) // 2
        self._acc_refresh_rects[account.id] = (icon_x, icon_y, icon_s, icon_s)
        self._draw_refresh_icon(painter, icon_x, icon_y, icon_s,
                                spinning=account.id in self._refreshing_ids)
        right_edge = icon_x - 6
        time_text = self._data_time_text(snapshot)
        if time_text:
            tf = QFont(); tf.setPointSize(8); painter.setFont(tf)
            tw = painter.fontMetrics().horizontalAdvance(time_text)
            painter.setPen(COLOR_TEXT_GRAY)
            painter.drawText(right_edge - tw, y, tw, self.ACCOUNT_HEADER_H,
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, time_text)
            right_edge -= tw + 8
        painter.setPen(COLOR_TEXT_WHITE)
        painter.setFont(f)

        # 套餐/计划备注(GLM 等级、火山 Plan 类型、Copilot 套餐+重置日、DeepSeek 多币种…)
        # 与标题同行、右侧 8pt 灰字。防叠字三保险:
        #   1) 标题宽用标题自己的字体量(10pt bold);
        #   2) 备注超宽先 elide;
        #   3) 最终再自检一次:备注左端必须在标题右端 + 8px 之后,不满足就整条不画。
        # (第 3 条是硬保障——字体/DPI 换环境时前两条的数学可能失准,实测踩过)
        msg = snapshot.credential_message if (snapshot and snapshot.success) else None
        note_text = None
        if msg:
            painter.setFont(f)
            title_w = painter.fontMetrics().horizontalAdvance(title)
            mf = QFont(); mf.setPointSize(8); painter.setFont(mf)
            mfm = painter.fontMetrics()
            max_w = right_edge - title_w - 12 - 16
            if max_w > 60:
                cand = mfm.elidedText(msg, Qt.TextElideMode.ElideRight, max_w)
                if 36 + title_w + 8 <= right_edge - mfm.horizontalAdvance(cand):
                    note_text = cand
            painter.setFont(f)
            painter.setPen(COLOR_TEXT_WHITE)

        if note_text is not None:
            nf = QFont(); nf.setPointSize(8)
            painter.setFont(nf)
            mfm = painter.fontMetrics()
            painter.setPen(COLOR_TEXT_GRAY)
            nx = right_edge - mfm.horizontalAdvance(note_text)
            painter.drawText(nx, y, mfm.horizontalAdvance(note_text),
                             self.ACCOUNT_HEADER_H,
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             note_text)
            painter.setFont(f)
            painter.setPen(COLOR_TEXT_WHITE)

        painter.drawText(36, y, right_edge - 36, self.ACCOUNT_HEADER_H,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         title)
        y += self.ACCOUNT_HEADER_H

        if snapshot and snapshot.success and snapshot.tiers:
            for tier in snapshot.tiers:
                y = self._draw_quota_item(painter, y, tier)
        elif snapshot and snapshot.is_transient:
            painter.setPen(COLOR_TEXT_GRAY)
            f = QFont(); f.setPointSize(9); painter.setFont(f)
            painter.drawText(18, y, width - 36, self.TIER_H,
                             Qt.AlignmentFlag.AlignVCenter, "暂时连不上,显示上次的值…")
            y += self.TIER_H
        elif snapshot and not snapshot.success:
            painter.setPen(QColor(244, 67, 54))
            f = QFont(); f.setPointSize(9); painter.setFont(f)
            painter.drawText(18, y, width - 36, self.TIER_H,
                             Qt.AlignmentFlag.AlignVCenter,
                             f"获取失败:{snapshot.error_message or '未知错误'}")
            y += self.TIER_H
        else:
            painter.setPen(COLOR_TEXT_GRAY)
            f = QFont(); f.setPointSize(9); painter.setFont(f)
            painter.drawText(18, y, width - 36, self.TIER_H,
                             Qt.AlignmentFlag.AlignVCenter, "等待数据…")
            y += self.TIER_H
        return y + self.ACCOUNT_GAP

    def _draw_quota_item(self, painter: QPainter, y: int, tier: QuotaTier) -> int:
        width = self.width()
        painter.setPen(COLOR_TEXT_SECONDARY)
        f = QFont(); f.setPointSize(9); painter.setFont(f)
        name_text = tier_display_name(tier)
        painter.drawText(22, y, width - 44, 16,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         name_text)
        # 金额档失效(如 DeepSeek is_available=False 余额耗尽):档名旁画红色 "!",
        # 值仍如实显示——警示"这行额度已不可用",不是隐藏它
        if tier.kind == KIND_MONEY and tier.is_valid is False:
            fm9 = painter.fontMetrics()
            nx = 22 + fm9.horizontalAdvance(name_text) + 4
            painter.setPen(QColor(244, 67, 54))
            wf = QFont(); wf.setPointSize(9); wf.setBold(True); painter.setFont(wf)
            painter.drawText(nx, y, 14, 16,
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "!")

        bar_y = y + 20
        bar_x = 22
        bar_width = width - 44 - 64
        bar_height = 8
        if bar_width < 40:
            bar_width = 40

        if tier.kind == KIND_MONEY:
            # 纯钱包余额(DeepSeek/StepFun/Novita/SiliconFlow):只有剩余金额,
            # 没有 total/used —— 算不出真实进度,画一条空轨道只会误导
            # ("是不是没加载出来?")。干脆不画轨道,金额数值左对齐顶上去,
            # 行高不变(仍按 TIER_H),布局稳定。
            # 有 total+used 的(OpenRouter/Copilot)进度条是真进度,照常画。
            has_progress = (tier.total not in (None, 0) and tier.used is not None)
            if has_progress:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(COLOR_PROGRESS_BAR_BG)
                painter.drawRoundedRect(bar_x, bar_y, bar_width, bar_height, 4, 4)
                pct = max(0.0, min(100.0, tier.used / tier.total * 100.0))
                fw = int(bar_width * pct / 100)
                if fw > 0:
                    painter.setBrush(get_percentage_color(int(pct)))
                    painter.drawRoundedRect(bar_x, bar_y, fw, bar_height, 4, 4)
                val_text = format_money(tier.remaining, tier.unit)
                val_x = bar_x + bar_width + 8
            else:
                val_text = format_money(tier.remaining, tier.unit)
                val_x = bar_x
            painter.setPen(COLOR_TEXT_WHITE)
            pf = QFont(); pf.setPointSize(9); painter.setFont(pf)
            painter.drawText(val_x, bar_y - 5, width - 44 - val_x, 18,
                             Qt.AlignmentFlag.AlignLeft, val_text)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(COLOR_PROGRESS_BAR_BG)
            painter.drawRoundedRect(bar_x, bar_y, bar_width, bar_height, 4, 4)
            pct = tier.utilization
            if pct is not None:
                # 钳到 [0,100]:百分比档也可能 >100 或 <0,不钳会让进度条溢出/下溢轨道
                clamped = max(0.0, min(100.0, float(pct)))
                fw = int(bar_width * clamped / 100)
                if fw > 0:
                    painter.setBrush(get_percentage_color(int(clamped)))
                    painter.drawRoundedRect(bar_x, bar_y, fw, bar_height, 4, 4)
            painter.setPen(COLOR_TEXT_WHITE)
            pf = QFont(); pf.setPointSize(9); painter.setFont(pf)
            painter.drawText(bar_x + bar_width + 8, bar_y - 5, 72, 18,
                             Qt.AlignmentFlag.AlignLeft,
                             f"{int(pct)}%" if pct is not None else "--%")

        if tier.resets_at:
            painter.setPen(COLOR_TEXT_GRAY)
            tf = QFont(); tf.setPointSize(8); painter.setFont(tf)
            painter.drawText(bar_x, bar_y + 11, bar_width, 14,
                             Qt.AlignmentFlag.AlignLeft, f"重置 {tier.resets_at}")
        # 无论有无重置时间,行高都按 TIER_H 走(有第三层就高、没有也统一,布局稳定)
        return y + self.TIER_H

    # ---- 设置入口 / 退出设置按钮 ----
    def _draw_gear_button(self, painter: QPainter):
        import math
        width = self.width()
        x = width - self.GEAR_SIZE - self.GEAR_MARGIN
        y = self.GEAR_MARGIN
        self._gear_rect = int(x), int(y), self.GEAR_SIZE, self.GEAR_SIZE
        if self._show_settings:
            painter.setPen(QPen(QColor(200, 200, 200), 2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(x + 5, y + 5, x + self.GEAR_SIZE - 5, y + self.GEAR_SIZE - 5)
            painter.drawLine(x + self.GEAR_SIZE - 5, y + 5, x + 5, y + self.GEAR_SIZE - 5)
            return
        cx, cy = x + self.GEAR_SIZE / 2, y + self.GEAR_SIZE / 2
        outer = self.GEAR_SIZE / 2 - 2
        inner = outer * 0.5
        teeth, depth = 8, 3
        path = QPainterPath()
        for i in range(teeth):
            a1 = (2 * math.pi / teeth) * i
            a2 = (2 * math.pi / teeth) * (i + 0.5)
            x1, y1 = cx + outer * math.cos(a1), cy + outer * math.sin(a1)
            x2, y2 = cx + (outer - depth) * math.cos(a1 + 0.1), cy + (outer - depth) * math.sin(a1 + 0.1)
            x3, y3 = cx + (outer - depth) * math.cos(a2 - 0.1), cy + (outer - depth) * math.sin(a2 - 0.1)
            x4, y4 = cx + outer * math.cos(a2), cy + outer * math.sin(a2)
            if i == 0:
                path.moveTo(x1, y1)
            else:
                path.lineTo(x1, y1)
            path.lineTo(x2, y2)
            path.lineTo(x3, y3)
            path.lineTo(x4, y4)
        path.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(cx - inner, cy - inner, inner * 2, inner * 2)
        painter.setPen(QPen(QColor(200, 200, 200), 1.5))
        painter.setBrush(QColor(200, 200, 200))
        painter.drawPath(path.subtracted(hole))

    def _draw_scrollbar(self, painter: QPainter, top: int):
        """内容可滚动时在右侧画一条细滚动指示条"""
        max_scroll = self._max_scroll()
        if max_scroll == 0:
            return
        track_h = self.height() - top - 8
        thumb_h = max(24, int(track_h * self.height() / self._content_height))
        thumb_y = top + int((track_h - thumb_h) * self._scroll / max_scroll)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 70))
        painter.drawRoundedRect(self.width() - 10, thumb_y, 4, thumb_h, 2, 2)

    # ---- 单账号刷新图标(每个账号标题行最右侧) ----
    ACC_REFRESH_SIZE = 16

    def _draw_refresh_icon(self, painter: QPainter, x: int, y: int, s: int, spinning: bool):
        import math
        cx, cy = x + s / 2, y + s / 2
        painter.save()
        if spinning:
            # 绕图标中心顺时针旋转(与箭头方向一致),刷新中用亮色
            painter.translate(cx, cy)
            painter.rotate(self._spin_angle)
            painter.translate(-cx, -cy)
        color = QColor(120, 200, 140) if spinning else QColor(170, 170, 170)
        r = s / 2 - 2.5
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 300° 圆弧(Qt 角度单位 1/16 度,0° 在 3 点钟、逆时针为正),缺口在右上
        painter.drawArc(int(cx - r), int(cy - r), int(2 * r), int(2 * r), 60 * 16, 300 * 16)
        # 弧起点(60°)处画箭头
        a = math.radians(60)
        ex, ey = cx + r * math.cos(a), cy - r * math.sin(a)
        tx, ty = math.sin(a), math.cos(a)        # 顺时针切向(屏幕坐标,y 向下)
        nx, ny = math.cos(a), -math.sin(a)       # 径向
        head = QPainterPath()
        head.moveTo(ex + tx * 3.5, ey + ty * 3.5)
        head.lineTo(ex + nx * 3 - tx * 1.2, ey + ny * 3 - ty * 1.2)
        head.lineTo(ex - nx * 3 - tx * 1.2, ey - ny * 3 - ty * 1.2)
        head.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(head)
        painter.restore()

    @staticmethod
    def _hit(rect, pos) -> bool:
        return bool(rect) and (rect[0] <= pos.x() <= rect[0] + rect[2]
                               and rect[1] <= pos.y() <= rect[1] + rect[3])

    # ---- 看板拖拽排序 ----
    DRAG_THRESHOLD = 6

    def _span_at(self, y: int):
        """面板坐标 y 处的账号块(列表页、内容区内且可排序时),否则 None"""
        if self._show_settings or y < self.TOP_PADDING - 6:
            return None
        spans = self._block_spans()
        if len(spans) < 2:
            return None
        cy = y + self._scroll
        return next((sp for sp in spans if sp[1] <= cy < sp[2]), None)

    def _insert_index(self, y: int) -> int:
        """拖到面板坐标 y 时的插入位置:越过某块中线就排到它后面"""
        cy = y + self._scroll
        return sum(1 for _a, top, bottom in self._block_spans() if (top + bottom) / 2 < cy)

    def _draw_drag_feedback(self, painter: QPainter):
        """拖动中:被拖的块加浅色底,插入位置画一条绿线(在滚动坐标系内画)"""
        if not (self._drag and self._drag["active"]):
            return
        spans = self._block_spans()
        for acc, top, bottom in spans:
            if acc.id == self._drag["id"]:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 28))
                painter.drawRoundedRect(8, top - 4, self.width() - 16, bottom - top - 6, 6, 6)
        i = self._drag["insert"]
        line_y = (spans[i][1] - 6) if i < len(spans) else (spans[-1][2] - 6)
        painter.setPen(QPen(QColor(76, 175, 80), 2))
        painter.drawLine(14, line_y, self.width() - 14, line_y)

    def _finish_drag(self):
        """松手:按插入位置重排;隐藏(未勾选)的账号在完整列表里保持原位"""
        drag, self._drag = self._drag, None
        self.unsetCursor()
        self.update()
        if not (drag and drag["active"]):
            return
        shown = [a.id for a, _t, _b in self._block_spans()]
        src = shown.index(drag["id"])
        dst = drag["insert"] - (1 if drag["insert"] > src else 0)
        if dst == src:
            return
        shown.insert(dst, shown.pop(src))
        full = [a.id for a in self._accounts]
        shown_set = set(shown)
        slots = [i for i, aid in enumerate(full) if aid in shown_set]
        for slot, aid in zip(slots, shown):
            full[slot] = aid
        self.accounts_reordered.emit(full)

    def _refresh_icon_at(self, pos) -> Optional[str]:
        """pos 处的单账号刷新图标对应的 account_id(列表页内容区内),否则 None"""
        if self._show_settings or pos.y() < self.TOP_PADDING - 6:
            return None
        cpos = QPoint(pos.x(), pos.y() + self._scroll)
        pad = 3   # 图标小,点击区域放宽几像素
        for aid, (x, y, w, h) in self._acc_refresh_rects.items():
            if self._hit((x - pad, y - pad, w + 2 * pad, h + 2 * pad), cpos):
                return aid
        return None

    def mousePressEvent(self, event):
        pos = event.pos()
        refresh_id = self._refresh_icon_at(pos)
        if self._hit(self._gear_rect, pos):
            self.toggle_settings()
        elif refresh_id is not None:
            if refresh_id not in self._refreshing_ids:
                self.account_refresh_clicked.emit(refresh_id)
        elif event.button() == Qt.MouseButton.LeftButton:
            span = self._span_at(pos.y())
            if span is not None:
                self._drag = {"id": span[0].id, "start_y": pos.y(),
                              "active": False, "insert": 0}
        event.accept()

    def mouseMoveEvent(self, event):
        y = event.pos().y()
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._drag["active"] and abs(y - self._drag["start_y"]) > self.DRAG_THRESHOLD:
                self._drag["active"] = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._drag["active"]:
                self._drag["insert"] = self._insert_index(y)
                self.update()
        elif self._hit(self._gear_rect, event.pos()) or self._refresh_icon_at(event.pos()) is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif self._span_at(y) is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_drag()
        event.accept()

    def leaveEvent(self, event):
        if self._drag is None:
            self.unsetCursor()
        super().leaveEvent(event)
