"""
更新弹窗与后台线程 (update_dialog)
==================================

【这个文件做什么】
自动更新的 Qt 层:检查/下载两个后台线程(QRunnable)+ 两个对话框。
网络与文件逻辑全部委托给 utils/updater(纯逻辑层),这里只管
"在哪个线程跑、结果怎么回到 GUI 线程、用户看到什么"。

【线程模型(与 poll_manager 同一套模式)】
  QRunnable.run() 在 QThreadPool 的工作线程里执行网络请求;
  它不能直接碰任何 QWidget——Qt 的规矩:GUI 对象只能在主线程动。
  所以 worker 用一个 QObject 持有信号,emit 后经"排队连接"自动
  切回主线程执行槽函数。跨线程只传"值"(dict/str/int),不传控件。

【两个对话框】
  UpdateDialog          "发现新版本":版本对比 + 发行说明 + 立即更新/本次跳过
                        (+ 可选"此版本不再提醒")
  UpdateProgressDialog  下载/解压进度条,支持取消(把 worker 的取消标志置位,
                        worker 在下一个分块边界注意到并放弃)
"""
import logging
import os
import re

from PySide6.QtCore import QObject, QRunnable, Signal, Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                               QProgressBar, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from utils import updater
from utils.version import APP_VERSION

logger = logging.getLogger(__name__)


# ============================================================
# 统一深色风格(与 AddAccountDialog / 设置页同一套设计语言):
# 背景 #222、输入区 #1f1f1f、主按钮绿 #4CAF50、次按钮灰 #333、圆角 4px
# ============================================================
_DL_QSS = (
    "QDialog{background:#222;color:#ddd;}"
    "QLabel{color:#ddd;border:none;}"
    "QPlainTextEdit{background:#1f1f1f;color:#cfcfcf;border:1px solid #3a3a3a;"
    "border-radius:4px;font-size:10px;padding:6px;}"
    "QProgressBar{background:#2a2a2a;border:1px solid #3a3a3a;border-radius:4px;"
    "text-align:center;color:#ddd;font-size:10px;min-height:12px;max-height:14px;}"
    "QProgressBar::chunk{background:#4CAF50;border-radius:3px;}"
)

# 主按钮(绿,同"验证并保存")/ 次按钮(灰,同"关闭服务")
_PRIMARY_BTN_QSS = (
    "QPushButton{background:#4CAF50;color:white;border:none;border-radius:4px;"
    "padding:6px 18px;font-size:10px;} QPushButton:hover{background:#45A049;}"
    "QPushButton:disabled{background:#555;}")
_SECONDARY_BTN_QSS = (
    "QPushButton{background:#333;color:white;border:none;border-radius:4px;"
    "padding:6px 14px;font-size:10px;} QPushButton:hover{background:#444;}")

# 复选框样式同设置页"开机自启动"
_SKIP_CHECKBOX_QSS = (
    "QCheckBox{color:#cfcfcf;font-size:10px;}"
    "QCheckBox::indicator{width:15px;height:15px;border:1px solid #969696;"
    "border-radius:2px;background:#282828;}"
    "QCheckBox::indicator:checked{background:#4CAF50;border:1px solid #4CAF50;}")


# ============================================================
# 后台 worker:网络一律不占 GUI 线程
# ============================================================
class _UpdaterSignals(QObject):
    """QRunnable 不能自定义信号,老规矩:配一个 QObject 专管发信号。"""
    checked = Signal(object)          # UpdateCheckWorker:结果 dict 或 None
    progress = Signal(int, int, str)  # UpdateDownloadWorker:(已下载, 总量, 阶段)
    succeeded = Signal(str)           # 下载+校验+解压完成:staging 里的新版目录
    failed = Signal(str)              # 失败:中文错误信息


class UpdateCheckWorker(QRunnable):
    """后台检查更新:请求 Gitee → 挑附件 → 比版本 → 解析 SHA256。

    run() 里一切异常都吞掉、发 checked(None)——自动检查的静默契约在
    updater.latest_release 内部已经保证了,这里再兜一层防意料之外。
    """

    def __init__(self):
        super().__init__()
        self.signals = _UpdaterSignals()

    def run(self):
        result = None
        try:
            release = updater.latest_release()
            if release is not None:
                asset = updater.pick_asset(release)
                if asset is None:
                    # 有发行版但没有我们的 zip(比如只发了源码 tag)→ 视为无更新
                    logger.info("最新发行版 %s 里没有安装包附件,视为无更新",
                                release.get("tag_name"))
                elif updater.is_newer(release["tag_name"], APP_VERSION):
                    result = {
                        "tag": release["tag_name"],
                        "notes": release.get("body") or "",
                        "asset_url": asset["browser_download_url"],
                        "zip_name": asset.get("name") or "",
                        "sha256": updater.parse_sha256(release.get("body") or ""),
                    }
        except Exception:
            logger.exception("检查更新线程异常")
            result = None
        self.signals.checked.emit(result)


class UpdateDownloadWorker(QRunnable):
    """后台执行更新:下载 → 校验 → 解压到 staging。

    取消机制:GUI 线程调用 cancel(),这里只置一个 bool(原子读写,
    不需要锁);download/extract 在每个分块/每个条目边界检查它。
    """

    def __init__(self, asset_url: str, zip_name: str, sha256):
        super().__init__()
        self.signals = _UpdaterSignals()
        self.asset_url = asset_url
        self.zip_name = zip_name          # 远端附件名,本地落盘沿用
        self.sha256 = sha256
        self._cancelled = False

    def cancel(self):
        """GUI 线程调用:请求取消。"""
        self._cancelled = True

    def run(self):
        s = self.signals
        try:
            p = updater.update_paths()
            # zip 存放名沿用远端附件名(如 AI额度悬浮窗-v1.0.1.zip),
            # cleanup/ps1 清理段按 *.zip 通配,不依赖具体版本号
            zip_path = os.path.join(p["update_dir"], self.zip_name)
            updater.ensure_disk_space(zip_path)

            # 阶段 1:下载(进度按 1MB 粒度回调,免得刷爆 GUI 线程)
            self.signals.progress.emit(0, 0, "下载")
            def _prog(done, total):
                s.progress.emit(done, total, "下载")
            size = updater.download_to(
                self.asset_url, zip_path, progress=_prog,
                cancel_check=lambda: self._cancelled)

            # 阶段 2:校验(SHA256 行缺失则跳过哈希,只查大小)
            updater.verify_download(zip_path, self.sha256, size)

            # 阶段 3:解压到 staging(每个 zip 条目边界响应取消)
            self.signals.progress.emit(0, 0, "解压")
            staging = updater.extract_zip(
                zip_path, p["staging_dir"],
                cancel_check=lambda: self._cancelled)

            s.succeeded.emit(staging)
        except updater.UpdateError as e:
            s.failed.emit(str(e))
        except Exception as e:
            logger.exception("下载更新线程异常")
            s.failed.emit(f"更新失败:{e}")


# ============================================================
# 对话框
# ============================================================
class UpdateDialog(QDialog):
    """「发现新版本」对话框(深色风格,与添加账号弹窗同一套设计语言)。

    - 标题行:"发现新版本" + 绿色新版本号
    - 版本行:当前 vX → 最新 vY(旧号灰、箭头灰、新号绿)
    - 中:发行说明(只读,末尾的 SHA256 校验行是给程序看的,过滤掉不显示)
    - 下:"此版本不再提醒"复选框 + [立即更新](绿主按钮) [本次跳过](灰次按钮)

    【结果怎么取】不用信号——模态 exec() 期间发信号会在"对话框还开着"
    的状态下触发槽,嵌套一层窗口模态,时序别扭。exec() 返回后直接问:
      update_accepted()  用户点了"立即更新"
      skip_checked()     勾了"此版本不再提醒"(配合"本次跳过"写入 skip)
    """

    def __init__(self, current_version: str, tag: str, notes: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(420)
        self._accepted_update = False
        self.setStyleSheet(_DL_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        # 标题:小节标题样式(同设置页"显示模式"),后缀绿色版本号点缀
        title = QLabel(f'发现新版本  <span style="color:#4CAF50;">{tag}</span>')
        title.setStyleSheet("color:#e0e0e0;font-size:12px;font-weight:bold;border:none;")
        layout.addWidget(title)

        # 版本对比行:旧号灰 → 新号绿,一眼看出"从哪到哪"
        head = QLabel(f'v{current_version}  <span style="color:#969696;">→</span>'
                      f'  <span style="color:#4CAF50;font-weight:bold;">{tag}</span>')
        head.setStyleSheet("color:#cfcfcf;font-size:11px;border:none;")
        head.setWordWrap(True)
        layout.addWidget(head)

        # 发行说明:去掉 SHA256 行再展示(那行是给更新器读的校验指纹)
        shown = "\n".join(
            line for line in (notes or "").splitlines()
            if not re.match(r"^SHA256:\s*[0-9a-fA-F]{64}\s*$", line))
        body = QPlainTextEdit(shown.strip() or "(本版本没有填写发行说明)")
        body.setReadOnly(True)
        body.setMaximumHeight(220)
        layout.addWidget(body, 1)

        self.skip_checkbox = QCheckBox("此版本不再提醒")
        self.skip_checkbox.setStyleSheet(_SKIP_CHECKBOX_QSS)
        layout.addWidget(self.skip_checkbox)

        btn_update = QPushButton("立即更新")
        btn_update.setDefault(True)
        btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_update.setStyleSheet(_PRIMARY_BTN_QSS)
        btn_update.clicked.connect(self._on_update)
        btn_skip = QPushButton("本次跳过")
        btn_skip.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_skip.setStyleSheet(_SECONDARY_BTN_QSS)
        btn_skip.clicked.connect(self._on_skip)

        # 按钮横排:右对齐,弹簧在左(同添加账号弹窗的布局习惯)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(btn_update)
        btns.addWidget(btn_skip)
        layout.addLayout(btns)

    def _on_update(self):
        self._accepted_update = True
        self.accept()

    def _on_skip(self):
        self.reject()

    def update_accepted(self) -> bool:
        """用户是否点了「立即更新」。"""
        return self._accepted_update

    def skip_checked(self) -> bool:
        """用户是否勾选了"此版本不再提醒"(配合"本次跳过"使用)。"""
        return self.skip_checkbox.isChecked()


class UpdateProgressDialog(QDialog):
    """下载/解压进度对话框(深色风格 QWidget,弃用系统样式 QProgressDialog)。

    结构:标题行(阶段) + 进度条 + 数值行(已下载/总量) + [取消]按钮。
    - 进度条用项目统一的分级色(<30% 绿…70%+ 红?不——这里是"完成度",
      越多越好,固定用主绿 #4CAF50,不套用额度分级色)。
    - 忙碌态(总量未知/解压中):进度条显示来回滑动的观感——Qt 自带
      QProgressBar setRange(0,0) 就是忙碌动画,直接复用,不用手写。
    - 取消 → 回调 on_cancel(由 FloatWindow 转发给 worker.cancel());
      "已取消"属于用户主动行为,失败弹窗链路里会被静默处理。
    """

    def __init__(self, on_cancel, parent=None):
        super().__init__(parent)
        self.setWindowTitle("正在更新")
        self.setModal(True)              # 下载期间锁住主窗口,行为同旧 QProgressDialog
        self.setMinimumWidth(380)
        self.setFixedHeight(148)
        self.setStyleSheet(_DL_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        # 阶段标题(小节标题样式;"正在下载…" / "正在解压…")
        self.phase_lbl = QLabel("正在下载…")
        self.phase_lbl.setStyleSheet(
            "color:#e0e0e0;font-size:12px;font-weight:bold;border:none;")
        lay.addWidget(self.phase_lbl)

        # 进度条:初始忙碌态(总大小未知)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)

        # 数值行:左"已下载 x.x MB",右"总 xx.x MB"(总量未知时只显左边)
        num_row = QHBoxLayout()
        self.done_lbl = QLabel("0.0 MB")
        self.done_lbl.setStyleSheet("color:#cfcfcf;font-size:10px;border:none;")
        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet("color:#969696;font-size:10px;border:none;")
        num_row.addWidget(self.done_lbl)
        num_row.addStretch(1)
        num_row.addWidget(self.total_lbl)
        lay.addLayout(num_row)

        lay.addStretch(1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet(_SECONDARY_BTN_QSS)
        self.cancel_btn.clicked.connect(self._cancel_clicked)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self._on_cancel_cb = on_cancel
        self._cancelled = False

    def _cancel_clicked(self):
        if self._cancelled:
            return
        self._cancelled = True
        if self._on_cancel_cb:
            self._on_cancel_cb()
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("正在取消…")
        self.phase_lbl.setText("正在取消…")

    def on_progress(self, done: int, total: int, phase: str):
        """worker 的 progress 信号直连到这里(自动排队回主线程)。"""
        mb = 1024 * 1024
        if phase == "下载":
            if total > 0:
                self.bar.setRange(0, 100)
                self.bar.setValue(int(done * 100 / total))
                self.done_lbl.setText(f"已下载 {done / mb:.1f} MB")
                self.total_lbl.setText(f"共 {total / mb:.1f} MB")
            else:
                # Gitee 响应头没给 Content-Length:退化为忙碌态 + 只显已下载量
                self.bar.setRange(0, 0)
                self.done_lbl.setText(f"已下载 {done / mb:.1f} MB")
                self.total_lbl.setText("")
        else:
            self.bar.setRange(0, 0)
            self.done_lbl.setText("")
            self.total_lbl.setText("")
        self.phase_lbl.setText("正在下载…" if phase == "下载" else "正在解压…")
