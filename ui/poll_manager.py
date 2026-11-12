"""
轮询管理器 (poll_manager)
=========================

【这个文件做什么】
统一调度"所有启用账号"的定时额度刷新,替代旧版 _QuotaRefreshWorker(它只盯一个 Key)。
核心是 PollManager:一个共享 QTimer + 一个 QThreadPool,到点为每个启用账号
各起一个 AccountFetchWorker 并发去取,取完批量回传给 UI。

【为什么是"一个定时器 + 线程池扇出",而不是每个账号一个定时器】
  - N 个定时器各自独立触发,容易撞在一起形成请求风暴,负载不可控。
  - 一个定时器统一节拍,线程池(并发上限 4)天然限流,超出的排队,稳。
  - 刷新间隔只有一处读取(config.refresh.interval_minutes),改一处生效。

【keep-last-good(cc-switch 的瞬时错误纪律)】
取额度可能因为网络抖动临时失败。这种"瞬时错误"下不该把悬浮窗清空/闪烁,
而应保留上一次的好值。所以:
  - provider 抛 TransientFetchError → worker 包成 is_transient=True 的失败快照。
  - PollManager 收到瞬时失败:若之前有成功快照,就【不动它】(继续显示旧值,
    queried_at 仍是旧时间 → UI 可据此提示"显示的是 N 分钟前的值");
    若没有旧值,才显示失败。
  - 确定性失败(401/3、业务码错)→ 正常替换,UI 显示错误。

【关键概念:Qt 跨线程信号】
QRunnable.run() 在线程池的工作线程里跑;它通过 signals.result_ready 发信号。
PollManager 住在主(GUI)线程,所以槽函数 _on_one 经"排队连接"回到主线程执行,
共享状态(_snapshots / _pending)只在主线程改,无需加锁。
"""
import logging
import time

from PySide6.QtCore import QObject, QTimer, QThreadPool, QRunnable, Signal, Slot

from api.models import QuotaSnapshot, TransientFetchError
from api.registry import get_provider
from utils.account_store import AccountStoreError

logger = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    """QRunnable 自己不能定义信号,用一个 QObject 持有信号。"""
    result_ready = Signal(str, object)  # (account_id, QuotaSnapshot)


class AccountFetchWorker(QRunnable):
    """
    单个账号的单次取额度任务(跑在线程池工作线程里)。

    【职责】只做网络 fetch + 异常归类;解析已在 provider.fetch 内完成。
    【为什么解析放在工作线程】解析很快,放工作线程不阻塞 GUI;
       旧版 _QuotaRefreshWorker 也是只把网络放后台、解析回主线程——
       这里因为 provider.fetch 把网络+解析合一,一起放后台更简单,对 GUI 无影响。
    """
    def __init__(self, account, provider, credentials):
        super().__init__()
        self.account = account
        self.provider = provider
        self.credentials = credentials
        self.signals = _WorkerSignals()

    def run(self):
        account_id = self.account.id
        provider_id = self.account.provider_id
        try:
            snapshot = self.provider.fetch(
                self.credentials, base_url=self.account.base_url_override)
            # provider 不知账号身份/时间,这里补上
            snapshot.account_id = account_id
            snapshot.provider_id = provider_id
            snapshot.queried_at = int(time.time() * 1000)
            self.signals.result_ready.emit(account_id, snapshot)
        except TransientFetchError as e:
            # 瞬时错误:包成 is_transient 快照,让 PollManager 决定是否保留旧值
            snap = QuotaSnapshot.failure_snapshot(
                provider_id, credential_status="valid",
                error_message=str(e), is_transient=True)
            snap.account_id = account_id
            snap.queried_at = int(time.time() * 1000)
            self.signals.result_ready.emit(account_id, snap)
        except Exception as e:
            # provider 实现不应抛到这里,但兜底:按确定性失败处理
            logger.warning("账号取额度异常(%s)", type(e).__name__)
            snap = QuotaSnapshot.failure_snapshot(
                provider_id, credential_status="parse_error",
                error_message="取额度失败，请检查平台配置和网络")
            snap.account_id = account_id
            snap.queried_at = int(time.time() * 1000)
            self.signals.result_ready.emit(account_id, snap)
        finally:
            self.provider.close()
            self.credentials.clear()


class VerifyCredentialsWorker(QRunnable):
    """
    "验证并添加账号"用的单次取额度任务。

    和 AccountFetchWorker 的区别:账号还没存进 AccountStore,只有 provider_id +
    凭据;emit 的 snapshot.success 直接告诉 UI 这组凭据能不能用。
    """
    def __init__(self, provider_id, credentials, base_url=None):
        super().__init__()
        self.provider_id = provider_id
        self.credentials = credentials
        self.base_url = base_url
        self.signals = _WorkerSignals()

    def run(self):
        provider = None
        try:
            provider = get_provider(self.provider_id)
            snapshot = provider.fetch(self.credentials, base_url=self.base_url)
            snapshot.provider_id = self.provider_id
            snapshot.queried_at = int(time.time() * 1000)
            # account_id 未知,用占位;UI 只关心 success/tiers
            snapshot.account_id = "__verify__"
            self.signals.result_ready.emit("__verify__", snapshot)
        except TransientFetchError as e:
            snap = QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="valid",
                error_message=str(e), is_transient=True)
            self.signals.result_ready.emit("__verify__", snap)
        except Exception as e:
            snap = QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error",
                error_message="验证失败，请检查平台配置和网络")
            self.signals.result_ready.emit("__verify__", snap)
        finally:
            if provider is not None:
                provider.close()


class PollManager(QObject):
    """多账号轮询调度器。"""

    # 一轮全部账号取完后,批量把 {account_id: QuotaSnapshot} 发给 UI
    snapshots_ready = Signal(dict)

    def __init__(self, account_store, interval_ms: int, parent=None):
        super().__init__(parent)
        self._store = account_store
        self._interval_ms = interval_ms

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self._pool = QThreadPool()
        # 并发上限 4:既快又不会一次打出十几个请求
        self._pool.setMaxThreadCount(max(1, min(4, self._pool.maxThreadCount())))

        self._snapshots = {}     # account_id -> 最近一次快照(keep-last-good 用)
        self._pending = set()    # 本轮还在等的账号 id
        self._live_workers = set()  # 持有运行中的 worker 引用,防 Python 提前 GC 掉它的 signals
        self._inflight = set()      # 正在采集的账号 id(同一账号不并发采集)

    # ====== 生命周期 ======
    def start(self):
        """启动:先立刻取一次,再按间隔定时取。"""
        self._tick()
        self._timer.start(self._interval_ms)

    def stop(self):
        """停止定时器并清空等待中的任务(关窗时调)。"""
        self._timer.stop()
        self._pool.clear()
        self._live_workers.clear()
        self._inflight.clear()

    def set_interval(self, interval_ms: int):
        """修改刷新间隔(设置界面);运行中则按新间隔重新计时。"""
        self._interval_ms = interval_ms
        if self._timer.isActive():
            self._timer.start(interval_ms)

    def refresh_now(self):
        """立即触发一轮(用户手动刷新 / 添加账号后)。
        定时器重新计时:免得手动刷新后紧跟一轮自动刷新,连续请求撞上平台限流。"""
        if self._timer.isActive():
            self._timer.start(self._interval_ms)
        self._tick()

    # ====== 内部 ======
    def refresh_account(self, account_id: str) -> bool:
        """只刷新一个账号(看板上的单账号刷新)。账号不存在/已停用/正在采集时返回 False。"""
        account = self._store.get_account(account_id)
        if account is None or not account.enabled or account_id in self._inflight:
            return False
        self._pending.add(account_id)
        self._start_fetch(account)
        return True

    def _tick(self):
        enabled = self._store.list_enabled()
        enabled_ids = {a.id for a in enabled}
        self._snapshots = {aid: snap for aid, snap in self._snapshots.items() if aid in enabled_ids}
        logger.debug("轮询 %d 个启用账号", len(enabled))
        if not enabled:
            # 没有启用账号:把本轮(空)结果发出去,UI 会显示空态
            self._pending = set()
            self.snapshots_ready.emit(dict(self._snapshots))
            return

        # 还在采集中的账号(上一轮未完 / 单账号刷新中)跳过,避免同账号并发采集
        targets = [a for a in enabled if a.id not in self._inflight]
        self._pending |= {a.id for a in targets}
        for account in targets:
            self._start_fetch(account)

    def _start_fetch(self, account):
        """为一个账号起采集 worker;拿不到凭据/平台时直接回一个失败快照。"""
        try:
            if account.base_url_override or account.provider_id == "zenmux":
                raise AccountStoreError("试用版已停用自定义端点和 ZenMux")
            creds = self._store.get_credentials(account)
            provider = get_provider(account.provider_id)
        except AccountStoreError as e:
            self._on_one(account.id, QuotaSnapshot.failure_snapshot(
                account.provider_id, credential_status="parse_error",
                error_message=str(e)))
            return
        except Exception as e:
            logger.warning("平台不可用(%s)", type(e).__name__)
            self._on_one(account.id, QuotaSnapshot.failure_snapshot(
                account.provider_id, credential_status="parse_error",
                error_message=f"不支持的平台: {account.provider_id}"))
            return
        worker = AccountFetchWorker(account, provider, creds)
        self._live_workers.add(worker)   # 保活:防 run 发射信号前被 GC
        self._inflight.add(account.id)
        # 闭包捕获 worker,结果交付后再从集合移除
        worker.signals.result_ready.connect(
            lambda aid, snap, wk=worker: self._on_result(wk, aid, snap))
        worker.setAutoDelete(True)
        self._pool.start(worker)

    def _on_result(self, worker, account_id: str, snapshot: QuotaSnapshot):
        """worker 结果入口:先释放引用,再走 _on_one。"""
        self._live_workers.discard(worker)
        self._inflight.discard(account_id)
        self._on_one(account_id, snapshot)

    @Slot(str, object)
    def _on_one(self, account_id: str, snapshot: QuotaSnapshot):
        account = self._store.get_account(account_id)
        if account is None or not account.enabled:
            self._snapshots.pop(account_id, None)
            self._pending.discard(account_id)
            if not self._pending:
                self.snapshots_ready.emit(dict(self._snapshots))
            return  # 删除/停用之前已发出的请求不得让账号重新出现在看板。
        # 补全身份/时间(worker 已填,这里防御性兜底)
        snapshot.account_id = account_id
        if snapshot.queried_at is None:
            snapshot.queried_at = int(time.time() * 1000)

        if snapshot.is_transient:
            # keep-last-good:有旧的成功快照就不覆盖(继续显示旧值,queried_at 维持旧 → UI 可提示陈旧)
            if not (account_id in self._snapshots and self._snapshots[account_id].success):
                self._snapshots[account_id] = snapshot
            logger.info("账号瞬时失败,保留上次值")
        else:
            self._snapshots[account_id] = snapshot
            if snapshot.success:
                logger.info("[%s] 刷新成功(%d 档)", account_id, len(snapshot.tiers))
            else:
                logger.info("账号刷新失败")

        self._pending.discard(account_id)
        # 本轮所有账号都回来了 → 批量通知 UI
        if not self._pending:
            self.snapshots_ready.emit(dict(self._snapshots))

    def get_snapshot(self, account_id: str) -> QuotaSnapshot:
        """取某账号当前(最近)快照,供 UI 展开面板等场景即时读取。"""
        return self._snapshots.get(account_id)
