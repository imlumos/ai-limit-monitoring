"""
高级设置对话框 (SettingsDialog)
==============================

软件名称与图标 / 刷新间隔 / 更新策略 / 外观 / 代理 + 配置导入导出。
没有"确定"按钮:每项改完即校验、保存并立即生效;非法值不保存,在底部提示原因。

和主窗口的约定(window 需提供):
    window.config                      当前完整配置(读)
    window.apply_settings(overrides)   校验 + 写 data/settings.toml + 立即生效;非法抛 SettingsError
"""
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QColor, QImageReader
from PySide6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QVBoxLayout, QLabel,
                               QSpinBox, QCheckBox, QComboBox, QLineEdit, QPushButton,
                               QFileDialog, QGroupBox, QColorDialog, QScrollArea, QWidget)

from utils import app_settings
from utils.app_settings import SettingsError, MIN_INTERVAL, MAX_INTERVAL
from utils.proxy_config import (ProxySettings, ProxyConfigError, get_proxy_settings,
                                save_and_apply_proxy)

_PROXY_MODES = [("environment", "沿用系统/环境代理"), ("direct", "直连(不使用代理)"),
                ("custom", "自定义代理地址")]

_QSS = (
    "QDialog,QScrollArea,QWidget#settingsContent{background:#222;color:white;} QLabel{color:#ddd;}"
    "QGroupBox{color:#e0e0e0;font-weight:bold;border:1px solid #3a3a3a;border-radius:4px;"
    "margin-top:10px;padding:10px 8px 6px 8px;}"
    "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}"
    "QCheckBox{color:#ddd;}"
    "QSpinBox,QComboBox,QLineEdit{background:#2a2a2a;color:white;border:1px solid #555;"
    "border-radius:3px;padding:3px 6px;}"
    "QLineEdit:disabled{color:#777;}"
    "QComboBox QAbstractItemView{background:#2a2a2a;color:white;selection-background-color:#4CAF50;}"
    "QPushButton{background:#333;color:white;border:none;border-radius:4px;padding:6px 12px;}"
    "QPushButton:hover{background:#444;}")


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color:#8a8a8a; font-size:11px;")
    return lbl


class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.setWindowTitle("高级设置")
        self.setModal(True)
        self.resize(400, 0)
        self.setStyleSheet(_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("settingsContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(8)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)

        # ---- 软件名称与图标 ----
        g = QGroupBox("软件标识")
        f = QFormLayout(g)
        self.app_name_edit = QLineEdit()
        self.app_name_edit.setMaxLength(40)
        f.addRow("软件名称:", self.app_name_edit)
        icon_row = QHBoxLayout()
        self.icon_preview = QLabel()
        self.icon_preview.setFixedSize(24, 24)
        self.icon_btn = QPushButton("选择图标…")
        self.icon_lbl = QLabel()
        icon_row.addWidget(self.icon_preview)
        icon_row.addWidget(self.icon_btn)
        icon_row.addWidget(self.icon_lbl, 1)
        f.addRow("软件图标:", icon_row)
        self.reset_identity_btn = QPushButton("恢复默认名称和图标")
        self.shortcut_btn = QPushButton("创建桌面快捷方式")
        f.addRow(self.reset_identity_btn)
        f.addRow(self.shortcut_btn)
        f.addRow(_hint("名称和图标即时应用于窗口与托盘。图标会转换为 ICO 保存在 data 目录。"
                       "桌面快捷方式需按当前设置创建;EXE 文件名及内嵌图标不变。"))
        root.addWidget(g)

        # ---- 刷新 ----
        g = QGroupBox("刷新")
        f = QFormLayout(g)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(MIN_INTERVAL, MAX_INTERVAL)
        self.interval_spin.setSuffix(" 分钟")
        f.addRow("刷新间隔:", self.interval_spin)
        f.addRow(_hint("间隔太短可能触发平台限流(如 Claude 返回 429),建议不低于 3 分钟。"))
        root.addWidget(g)

        # ---- 更新 ----
        g = QGroupBox("更新")
        v = QVBoxLayout(g)
        self.update_enabled = QCheckBox("启用内置更新")
        self.update_on_startup = QCheckBox("启动时自动检查更新")
        v.addWidget(self.update_enabled)
        v.addWidget(self.update_on_startup)
        v.addWidget(_hint("更新包来自 Gitee 发行版,目前没有独立签名校验,"
                          "开启前请确认信任发布源。仅打包版可用。"))
        root.addWidget(g)

        # ---- 外观 ----
        g = QGroupBox("外观")
        f = QFormLayout(g)
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(90)
        f.addRow("背景颜色:", self.color_btn)
        self.opacity_spin = QSpinBox()
        self.opacity_spin.setRange(0, 100)
        self.opacity_spin.setSuffix(" %")
        f.addRow("不透明度:", self.opacity_spin)
        img_row = QHBoxLayout()
        self.image_btn = QPushButton("选择图片…")
        self.clear_image_btn = QPushButton("清除")
        self.image_lbl = QLabel()
        img_row.addWidget(self.image_btn)
        img_row.addWidget(self.clear_image_btn)
        img_row.addWidget(self.image_lbl, 1)
        f.addRow("背景图:", img_row)
        self.reset_look_btn = QPushButton("恢复默认外观")
        f.addRow("", self.reset_look_btn)
        f.addRow(_hint("图片会复制到 data 目录。有背景图时,背景颜色作为图上的遮罩,"
                       "不透明度调低图更清晰,调高文字更易读。"))
        root.addWidget(g)

        # ---- 代理 ----
        g = QGroupBox("网络代理")
        f = QFormLayout(g)
        self.proxy_mode = QComboBox()
        for mode, text in _PROXY_MODES:
            self.proxy_mode.addItem(text, mode)
        self.proxy_url = QLineEdit()
        self.proxy_url.setPlaceholderText("http://127.0.0.1:7890")
        f.addRow("代理模式:", self.proxy_mode)
        f.addRow("代理地址:", self.proxy_url)
        f.addRow(_hint("所有平台共用。支持 HTTP/HTTPS 代理,不支持 SOCKS 和带账号密码的地址。"))
        root.addWidget(g)

        # ---- 导入导出 ----
        row = QHBoxLayout()
        self.import_btn = QPushButton("导入配置…")
        self.export_btn = QPushButton("导出配置…")
        row.addWidget(self.import_btn)
        row.addWidget(self.export_btn)
        row.addStretch()
        root.addLayout(row)
        root.addWidget(_hint("导入导出包含以上设置、软件图标及背景图,不含账号和 API Key。"))

        self.status_lbl = QLabel(" ")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        self._load_values()

        self.app_name_edit.editingFinished.connect(self._save_app_name)
        self.icon_btn.clicked.connect(self._on_pick_app_icon)
        self.reset_identity_btn.clicked.connect(self._on_reset_identity)
        self.shortcut_btn.clicked.connect(self._on_create_shortcut)
        self.interval_spin.valueChanged.connect(
            lambda v: self._apply({"refresh": {"interval_minutes": v}}))
        self.update_enabled.toggled.connect(self._on_update_enabled)
        self.update_on_startup.toggled.connect(
            lambda c: self._apply({"update": {"check_on_startup": c}}))
        self.opacity_spin.valueChanged.connect(
            lambda v: self._apply({"appearance": {"bg_opacity": v}}))
        self.color_btn.clicked.connect(self._on_pick_color)
        self.image_btn.clicked.connect(self._on_pick_image)
        self.clear_image_btn.clicked.connect(self._on_clear_image)
        self.reset_look_btn.clicked.connect(self._on_reset_look)
        self.proxy_mode.currentIndexChanged.connect(self._on_proxy_mode)
        self.proxy_url.editingFinished.connect(self._save_proxy)
        self.import_btn.clicked.connect(self._on_import)
        self.export_btn.clicked.connect(self._on_export)
        self.resize(400, content.sizeHint().height())

    def showEvent(self, event):
        super().showEvent(event)
        # 显示后原生标题栏尺寸才可靠,定位时必须把标题栏和边框一起算入。
        self._fit_to_screen()
        QTimer.singleShot(0, self._fit_to_screen)

    def _fit_to_screen(self):
        available = self.screen().availableGeometry()
        frame = self.frameGeometry()
        border_width = frame.width() - self.width()
        border_height = frame.height() - self.height()
        self.resize(min(self.width(), max(1, available.width() - border_width)),
                    min(self.height(), max(1, available.height() - border_height)))
        frame = self.frameGeometry()
        x = max(available.left(), min(frame.left(), available.right() - frame.width() + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1))
        self.windowHandle().setFramePosition(QPoint(x, y))

    # ---- 把当前配置填进控件(不触发保存) ----
    def _load_values(self):
        cfg = self._window.config
        widgets = (self.app_name_edit, self.interval_spin, self.update_enabled, self.update_on_startup,
                   self.opacity_spin, self.proxy_mode, self.proxy_url)
        for w in widgets:
            w.blockSignals(True)
        identity = cfg.get("application", {})
        self.app_name_edit.setText(identity.get("name", app_settings.DEFAULT_APP_NAME))
        self._show_app_icon()
        self.interval_spin.setValue(cfg.get("refresh", {}).get("interval_minutes", 10))
        upd = cfg.get("update", {})
        self.update_enabled.setChecked(bool(upd.get("enabled", False)))
        self.update_on_startup.setChecked(bool(upd.get("check_on_startup", False)))
        self.update_on_startup.setEnabled(self.update_enabled.isChecked())
        look = cfg.get("appearance", {})
        self.opacity_spin.setValue(look.get("bg_opacity", 70))
        self._show_color(look.get("bg_color", "#000000"))
        self._show_image(look.get("bg_image", ""))
        proxy = get_proxy_settings()
        self.proxy_mode.setCurrentIndex(max(0, self.proxy_mode.findData(proxy.mode)))
        self.proxy_url.setText(proxy.url)
        self.proxy_url.setEnabled(proxy.mode == "custom")
        for w in widgets:
            w.blockSignals(False)

    def _status(self, ok: bool, text: str):
        self.status_lbl.setStyleSheet(f"color:{'#4CAF50' if ok else '#F44336'};")
        self.status_lbl.setText(text)

    def _apply(self, overrides: dict) -> bool:
        try:
            self._window.apply_settings(overrides)
        except SettingsError as e:
            self._status(False, str(e))
            return False
        self._status(True, "已保存并生效")
        return True

    # ---- 软件名称 / 图标 / 桌面快捷方式 ----
    def _save_app_name(self) -> bool:
        name = self.app_name_edit.text().strip()
        current = self._window.config.get("application", {}).get("name", app_settings.DEFAULT_APP_NAME)
        if name == current:
            return True
        if not self._apply({"application": {"name": name}}):
            return False
        self.app_name_edit.setText(self._window.config["application"]["name"])
        return True

    def _show_app_icon(self):
        name = self._window.config.get("application", {}).get("icon", "")
        self.icon_lbl.setText("自定义图标" if name else "默认图标")
        self.icon_preview.setPixmap(self._window.windowIcon().pixmap(24, 24))

    def _on_pick_app_icon(self):
        exts = " ".join("*" + ext for ext in app_settings.APP_ICON_EXTS)
        path, _ = QFileDialog.getOpenFileName(self, "选择软件图标", "", f"图标或图片 ({exts})")
        if path:
            self.set_app_icon(path)

    def set_app_icon(self, path: str) -> bool:
        try:
            data = app_settings.read_app_icon(path)
            with app_settings.installed_assets({app_settings.APP_ICON_FILE: data}):
                self._window.apply_settings({"application": {"icon": app_settings.APP_ICON_FILE}})
        except SettingsError as e:
            self._status(False, str(e))
            return False
        self._show_app_icon()
        self.setWindowIcon(self._window.windowIcon())
        self._status(True, "图标已保存并生效")
        return True

    def _on_reset_identity(self):
        if self._apply({"application": {"name": app_settings.DEFAULT_APP_NAME, "icon": ""}}):
            self._load_values()
            self.setWindowIcon(self._window.windowIcon())

    def _on_create_shortcut(self):
        from utils.app_identity import create_desktop_shortcut
        if not self._save_app_name():
            return
        try:
            create_desktop_shortcut(self._window.config)
        except SettingsError as e:
            self._status(False, str(e))
            return
        self._status(True, "桌面快捷方式已创建;修改名称或图标后可重新创建")

    # ---- 外观 ----
    def _show_color(self, color: str):
        text = "white" if QColor(color).lightness() < 128 else "black"
        self.color_btn.setText(color.upper())
        self.color_btn.setStyleSheet(f"QPushButton{{background:{color};color:{text};"
                                     "border:1px solid #555;}")

    def _show_image(self, name: str):
        self.image_lbl.setText("已设置" if app_settings.bg_image_path(name) else "未设置")
        self.clear_image_btn.setEnabled(bool(name))

    def _on_pick_color(self):
        current = QColor(self._window.config.get("appearance", {}).get("bg_color", "#000000"))
        color = QColorDialog.getColor(current, self, "背景颜色")
        if color.isValid() and self._apply({"appearance": {"bg_color": color.name()}}):
            self._show_color(color.name())

    def _on_pick_image(self):
        exts = " ".join("*" + e for e in app_settings.BG_IMAGE_EXTS)
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图", "", f"图片 ({exts})")
        if path:
            self.set_background_image(path)

    def set_background_image(self, path: str) -> bool:
        if not QImageReader(path).canRead():
            self._status(False, "无法读取该图片")
            return False
        try:
            name = app_settings.store_background_image(path)
        except SettingsError as e:
            self._status(False, str(e))
            return False
        ok = self._apply({"appearance": {"bg_image": name}})
        self._show_image(name if ok else "")
        return ok

    def _on_clear_image(self):
        if self._apply({"appearance": {"bg_image": ""}}):
            app_settings.remove_background_images()
            self._show_image("")

    def _on_reset_look(self):
        if self._apply({"appearance": {"bg_color": "#000000", "bg_opacity": 70, "bg_image": ""}}):
            app_settings.remove_background_images()
            self._load_values()

    def _on_update_enabled(self, checked: bool):
        self.update_on_startup.setEnabled(checked)
        self._apply({"update": {"enabled": checked}})

    def _on_proxy_mode(self, _index):
        custom = self.proxy_mode.currentData() == "custom"
        self.proxy_url.setEnabled(custom)
        if not custom:
            self.proxy_url.setText("")
        elif not self.proxy_url.text().strip():
            self._status(True, "填写代理地址后自动保存")
            self.proxy_url.setFocus()
            return
        self._save_proxy()

    def _save_proxy(self):
        mode = self.proxy_mode.currentData()
        url = self.proxy_url.text().strip() if mode == "custom" else ""
        if mode == "custom" and not url:
            return
        current = get_proxy_settings()
        if (current.mode, current.url) == (mode, url):
            return
        try:
            save_and_apply_proxy(ProxySettings(mode, url))
        except ProxyConfigError as e:
            self._status(False, str(e))
            return
        self._status(True, "代理已保存,下一次刷新起生效")

    # ---- 导入 / 导出 ----
    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置", "ai-quota-settings.toml",
                                              "TOML 配置 (*.toml)")
        if not path:
            return
        try:
            app_settings.export_settings(path, app_settings.current_overrides(self._window.config),
                                         get_proxy_settings())
        except SettingsError as e:
            self._status(False, str(e))
            return
        self._status(True, "已导出")

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入配置", "", "TOML 配置 (*.toml)")
        if not path:
            return
        self.import_from(path)

    def import_from(self, path: str) -> bool:
        """整体校验通过才写入;任何一项非法则什么都不改。"""
        assets = {}
        try:
            overrides, proxy = app_settings.import_settings(path, assets=assets)
        except SettingsError as e:
            self._status(False, f"导入失败:{e}")
            return False
        try:
            with app_settings.installed_assets(assets):
                previous_proxy = get_proxy_settings()
                if proxy is not None:
                    save_and_apply_proxy(proxy)
                try:
                    if overrides:
                        self._window.apply_settings(overrides)
                except SettingsError:
                    if proxy is not None:
                        save_and_apply_proxy(previous_proxy)
                    raise
        except (SettingsError, ProxyConfigError) as e:
            self._status(False, f"导入失败:{e}")
            return False
        self._load_values()
        self.setWindowIcon(self._window.windowIcon())
        self._status(True, "已导入并生效")
        return True
