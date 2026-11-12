"""安全回归:假凭据、临时目录、内存 HTTP Adapter,不访问真实账号或网络。"""
import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests
from requests.adapters import BaseAdapter
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

import api.providers  # 注册适配器,不读取凭据。
from api.models import DeterministicError, QuotaSnapshot, TransientFetchError
from api.provider_base import QuotaProvider
from api.registry import get_provider, list_providers
from api.transport import ALLOWED_HOSTS, ProviderSession
from ui.poll_manager import AccountFetchWorker, PollManager, VerifyCredentialsWorker
from utils import account_store as store_module
from utils import paths
from utils.account_store import Account, AccountStore, AccountStoreError, SERVICE_NAME
from utils.redaction import RedactingFormatter, redact


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    vault = {}
    monkeypatch.setattr(paths, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(store_module.keyring, "get_password", lambda s, u: vault.get((s, u)))
    monkeypatch.setattr(store_module.keyring, "set_password", lambda s, u, v: vault.__setitem__((s, u), v))
    monkeypatch.setattr(store_module.keyring, "delete_password", lambda s, u: vault.pop((s, u), None))
    monkeypatch.setattr(QuotaProvider, "read_cli_json", staticmethod(lambda *parts: None))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        Mock(side_effect=AssertionError("Real network forbidden in security tests")))
    return vault


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class CaptureAdapter(BaseAdapter):
    def __init__(self, body=None, status=200, headers=None):
        self.body = body if body is not None else {"balance_infos": [{"total_balance": "1"}]}
        self.status = status
        self.headers = headers or {}
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(self.body).encode("utf-8")
        response.encoding = "utf-8"
        response.request = request
        response.url = request.url
        response.headers.update(self.headers)
        return response

    def close(self):
        pass


def capture(session, **kwargs):
    adapter = CaptureAdapter(**kwargs)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return adapter


@pytest.mark.parametrize("url", [
    "http://api.deepseek.com/user/balance", "https://evil.invalid/",
    "https://api.deepseek.com.evil.invalid/", "https://api.deepseek.com@evil.invalid/",
    "https://user:secret@api.deepseek.com/", "https://api.deepseek.com:444/",
    "https://api.deepseek.com/#secret", "file:///secret", "https://[invalid",
])
def test_endpoint_rejected_before_transport(url):
    session = ProviderSession("deepseek")
    adapter = capture(session)
    with pytest.raises(DeterministicError):
        session.get(url, headers={"Authorization": "Bearer FAKE_SECRET"})
    assert not adapter.calls


@pytest.mark.parametrize("provider_id,host", [
    (pid, host) for pid, hosts in ALLOWED_HOSTS.items() for host in sorted(hosts)
])
def test_all_platform_default_hosts_allowed(provider_id, host):
    session = ProviderSession(provider_id)
    adapter = capture(session)
    session.get(f"https://{host}/")
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1]["verify"] is True


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_never_forwards_token_or_body(status):
    session = ProviderSession("gemini")
    adapter = capture(session, status=status, headers={"Location": "https://evil.invalid/"})
    response = session.post("https://oauth2.googleapis.com/token",
                            data={"refresh_token": "FAKE_REFRESH"}, allow_redirects=True)
    assert response.status_code == status
    assert len(adapter.calls) == 1


def test_proxy_environment_and_no_proxy_preserved(monkeypatch):
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:19876")
    session = ProviderSession("codex")
    assert session.trust_env is True
    adapter = capture(session)
    session.get("https://chatgpt.com/backend-api/wham/usage")
    assert adapter.calls[-1][1]["proxies"]["https"] == "http://127.0.0.1:19876"
    monkeypatch.setenv("NO_PROXY", "chatgpt.com")
    session.get("https://chatgpt.com/backend-api/wham/usage")
    assert not adapter.calls[-1][1]["proxies"].get("https")


def test_custom_override_and_zenmux_disabled():
    provider = get_provider("deepseek")
    adapter = capture(provider._session)
    assert not provider.fetch({"api_key": "FAKE"}, base_url="https://evil.invalid").success
    zenmux = get_provider("zenmux")
    assert not zenmux.fetch({"api_key": "FAKE", "base_url": "https://evil.invalid"}).success
    assert not adapter.calls
    assert "zenmux" not in {p["provider_id"] for p in list_providers()}


def test_account_sessions_do_not_share_cookie():
    first, second = get_provider("deepseek"), get_provider("deepseek")
    assert first is not second
    first._session.cookies.set("session", "ACCOUNT_A", domain="api.deepseek.com")
    adapter = capture(second._session)
    second.fetch({"api_key": "ACCOUNT_B"})
    request = adapter.calls[0][0]
    assert request.headers["Authorization"] == "Bearer ACCOUNT_B"
    assert "Cookie" not in request.headers


@pytest.mark.parametrize("provider_id,filename,data", [
    ("claude", (".claude", ".credentials.json"), {"claudeAiOauth": {"accessToken": "FAKE_CLI_TOKEN"}}),
    ("codex", (".codex", "auth.json"), {"auth_mode": "chatgpt", "tokens": {"access_token": "FAKE_CLI_TOKEN", "account_id": "FAKE_ACCOUNT"}}),
    ("gemini", (".gemini", "oauth_creds.json"), {"access_token": "FAKE_CLI_TOKEN", "expiry_date": 9999999999999}),
    ("grok", (".grok", "auth.json"), {"https://auth.x.ai::audit": {"key": "FAKE_CLI_TOKEN"}}),
])
def test_cli_credentials_still_used_only_at_platform_endpoint(provider_id, filename, data, monkeypatch):
    read = Mock(return_value=data)
    monkeypatch.setattr(QuotaProvider, "read_cli_json", staticmethod(read))
    provider = get_provider(provider_id)
    adapter = capture(provider._session, status=403)
    snapshot = provider.fetch({})
    read.assert_called_once_with(*filename)
    assert not snapshot.success
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0].headers["Authorization"] == "Bearer FAKE_CLI_TOKEN"
    if provider_id == "codex":
        assert adapter.calls[0][0].headers["ChatGPT-Account-Id"] == "FAKE_ACCOUNT"


def test_empty_response_cannot_verify_credentials():
    provider = get_provider("deepseek")
    capture(provider._session, body={})
    assert not provider.fetch({"api_key": "FAKE"}).success


def test_raw_business_error_does_not_reach_logs(caplog):
    provider = get_provider("glm")
    capture(provider._session, body={"code": 400, "msg": "ECHOED_FAKE_SECRET", "success": False})
    snapshot = provider.fetch({"api_key": "ECHOED_FAKE_SECRET"})
    assert not snapshot.success
    assert "ECHOED_FAKE_SECRET" not in caplog.text + snapshot.error_message


def test_known_secret_only_saved_in_keyring(isolation):
    store = AccountStore()
    account = store.add_account("deepseek", "test", {"api_key": "FAKE_SECRET"})
    assert store.get_credentials(account) == {"api_key": "FAKE_SECRET"}
    assert "FAKE_SECRET" not in Path(store._file_path()).read_text(encoding="utf-8")
    assert store.list_enabled() == [account]


@pytest.mark.parametrize("provider,creds", [
    ("unknown", {"api_key": "FAKE"}), ("deepseek", {"api_key": "FAKE", "extra_secret": "FAKE"}),
    ("deepseek", {}), ("deepseek", {"api_key": "FAKE\nHEADER"}),
])
def test_bad_schema_rejected_before_storage(provider, creds, isolation):
    store = AccountStore()
    with pytest.raises(AccountStoreError):
        store.add_account(provider, "test", creds)
    assert store.list_accounts() == []
    assert isolation == {}


def test_write_failure_remains_disabled_and_visible(monkeypatch, caplog):
    store = AccountStore()
    monkeypatch.setattr(store_module.keyring, "set_password", Mock(side_effect=RuntimeError("FAKE_SECRET")))
    with pytest.raises(AccountStoreError):
        store.add_account("deepseek", "test", {"api_key": "FAKE_SECRET"})
    assert store.list_enabled() == []
    assert len(AccountStore().list_accounts()) == 1
    assert AccountStore().list_enabled() == []
    assert "FAKE_SECRET" not in caplog.text


def test_write_readback_failure_is_not_success(monkeypatch):
    store = AccountStore()
    monkeypatch.setattr(store_module.keyring, "set_password", lambda *args: None)
    with pytest.raises(AccountStoreError):
        store.add_account("deepseek", "test", {"api_key": "FAKE"})
    assert not store.list_enabled()


def test_delete_failure_retains_disabled_record_for_retry(monkeypatch, isolation):
    store = AccountStore()
    account = store.add_account("deepseek", "test", {"api_key": "FAKE"})
    delete = store_module.keyring.delete_password
    monkeypatch.setattr(store_module.keyring, "delete_password", Mock(side_effect=RuntimeError("denied")))
    with pytest.raises(AccountStoreError):
        store.delete_account(account.id)
    assert not AccountStore().get_account(account.id).enabled
    assert (SERVICE_NAME, f"account:{account.id}:api_key") in isolation
    monkeypatch.setattr(store_module.keyring, "delete_password", delete)
    store.delete_account(account.id)
    assert AccountStore().get_account(account.id) is None
    assert isolation == {}


def test_json_failure_before_add_does_not_write_credentials(monkeypatch, isolation):
    store = AccountStore()
    monkeypatch.setattr(store_module.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(AccountStoreError):
        store.add_account("deepseek", "test", {"api_key": "FAKE"})
    assert not store.list_accounts()
    assert not isolation


def test_activation_save_failure_keeps_trackable_disabled_record(monkeypatch, isolation):
    store = AccountStore()
    save = store._save
    def fail_activation():
        if any(a.enabled for a in store.list_accounts()):
            raise AccountStoreError("fake save failure")
        save()
    monkeypatch.setattr(store, "_save", fail_activation)
    with pytest.raises(AccountStoreError):
        store.add_account("deepseek", "test", {"api_key": "FAKE"})
    assert not store.list_enabled()
    assert not AccountStore().list_enabled()
    assert len(store.list_accounts()) == 1
    assert len(isolation) == 1


def test_update_failure_does_not_delete_old_key(monkeypatch):
    store = AccountStore()
    account = store.add_account("deepseek", "test", {"api_key": "OLD_FAKE"})
    monkeypatch.setattr(store_module.keyring, "set_password", Mock(side_effect=RuntimeError("denied")))
    with pytest.raises(AccountStoreError):
        store.update_account(account.id, credentials={"api_key": "NEW_FAKE"})
    assert store.get_credentials(account)["api_key"] == "OLD_FAKE"
    assert not store.get_account(account.id).enabled


def test_migration_cleanup_and_delete_survive_restart(isolation):
    isolation[(SERVICE_NAME, "apikey")] = "LEGACY_FAKE"
    store = AccountStore()
    account = store.get_account("migrated-glm")
    assert account.enabled
    assert (SERVICE_NAME, "apikey") not in isolation
    # 模拟旧版本曾留下重复 legacy 条目,删除也应清理。
    isolation[(SERVICE_NAME, "apikey")] = "LEGACY_FAKE"
    store.delete_account(account.id)
    assert not AccountStore().list_accounts()
    assert not isolation


def test_migration_done_never_resurrects_deleted_account(isolation):
    store = AccountStore()
    isolation[(SERVICE_NAME, "apikey")] = "LEGACY_FAKE"
    assert not AccountStore().list_accounts()


def test_corrupt_account_file_not_overwritten():
    store = AccountStore()
    path = Path(store._file_path())
    path.write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(AccountStoreError):
        AccountStore()
    assert path.read_text(encoding="utf-8") == "invalid JSON"


@pytest.mark.parametrize("text,secret", [
    ("Authorization: Bearer ABC123", "ABC123"),
    ("Authorization: RAWKEY", "RAWKEY"),
    ('{"refresh_token": "FAKE_REFRESH"}', "FAKE_REFRESH"),
    ("api_key='FAKE_KEY'", "FAKE_KEY"),
    ("https://user:FAKE_PASSWORD@example.com/SECRET_PATH?key=FAKE_QUERY", "FAKE"),
    ("contact person@example.com", "person@example.com"),
    ("Cookie: session=FAKE_ONE; other=FAKE_TWO", "FAKE"),
])
def test_redaction(text, secret):
    assert secret not in redact(text)


def test_formatter_redacts_exception_and_interpolation():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    log = logging.Logger("security-test")
    log.addHandler(handler)
    try:
        raise ValueError("refresh_token=FAKE_EXCEPTION")
    except ValueError:
        log.exception("request %s", "https://user:FAKE_PASS@example.com/?key=FAKE_QUERY")
    assert "FAKE" not in stream.getvalue()


def test_worker_closes_session_and_clears_credentials(qapp):
    account = Account("test", "test", "deepseek")
    provider = get_provider("deepseek")
    capture(provider._session)
    provider.close = Mock(wraps=provider.close)
    credentials = {"api_key": "FAKE"}
    worker = AccountFetchWorker(account, provider, credentials)
    received = []
    worker.signals.result_ready.connect(lambda aid, snap: received.append(snap))
    worker.run()
    assert received[0].success
    provider.close.assert_called_once()
    assert credentials == {}


def test_verify_closes_session_but_keeps_credentials_for_save(qapp, monkeypatch):
    provider = get_provider("deepseek")
    capture(provider._session)
    provider.close = Mock(wraps=provider.close)
    monkeypatch.setattr("ui.poll_manager.get_provider", lambda pid: provider)
    credentials = {"api_key": "FAKE"}
    VerifyCredentialsWorker("deepseek", credentials).run()
    provider.close.assert_called_once()
    assert credentials == {"api_key": "FAKE"}


def test_late_worker_result_cannot_restore_deleted_account(qapp):
    store = AccountStore()
    account = store.add_account("deepseek", "test", {"api_key": "FAKE"})
    manager = PollManager(store, 60000)
    store.delete_account(account.id)
    manager._on_one(account.id, QuotaSnapshot.success_snapshot("deepseek", []))
    assert manager.get_snapshot(account.id) is None


def test_poll_credential_failure_returns_error(qapp, monkeypatch):
    store = AccountStore()
    account = store.add_account("deepseek", "test", {"api_key": "FAKE"})
    monkeypatch.setattr(store, "get_credentials", Mock(side_effect=AccountStoreError("vault unavailable")))
    manager = PollManager(store, 60000)
    manager.refresh_now()
    assert not manager.get_snapshot(account.id).success


def test_ui_add_failure_does_not_close_dialog(qapp):
    from ui.float_window import FloatWindow
    window = SimpleNamespace(
        account_store=SimpleNamespace(add_account=Mock(side_effect=AccountStoreError("save failed"))),
        _sync_panel_accounts=Mock(), _relayout=Mock())
    dialog = SimpleNamespace(set_status=Mock(), accept=Mock())
    FloatWindow._on_verify_done(window, "deepseek", "test", {"api_key": "FAKE"},
                                QuotaSnapshot.success_snapshot("deepseek", []), dialog)
    dialog.set_status.assert_called_once_with(False, "save failed")
    dialog.accept.assert_not_called()


def test_ui_delete_failure_warns_and_keeps_record(qapp, monkeypatch):
    from ui.float_window import FloatWindow, QMessageBox
    store = AccountStore()
    account = store.add_account("deepseek", "test", {"api_key": "FAKE"})
    monkeypatch.setattr(store_module.keyring, "delete_password", Mock(side_effect=RuntimeError("denied")))
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    window = SimpleNamespace(account_store=store, _snapshots={account.id: object()},
                             poll_manager=SimpleNamespace(_snapshots={account.id: object()}),
                             detail_panel=SimpleNamespace(set_snapshots=Mock()),
                             _sync_panel_accounts=Mock(), _relayout=Mock())
    FloatWindow._on_delete_account(window, account.id)
    assert not store.get_account(account.id).enabled
    warning.assert_called_once()


def test_updates_disabled_even_when_config_missing(qapp, monkeypatch):
    from ui.float_window import FloatWindow
    import tomllib
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "config/settings.toml").read_text(encoding="utf-8"))
    assert config["update"] == {"enabled": False, "check_on_startup": False}
    for settings in [config, {}, {"update": {"check_on_startup": True}}]:
        window = SimpleNamespace(config=settings)
        # 若门禁未生效,这些不完整的对象会进入后续 UI/网络流程并失败。
        FloatWindow._check_for_updates(window, manual=True)
        FloatWindow._on_update_confirmed(window, {})


def test_full_window_empty_startup_without_real_credentials(qapp, monkeypatch):
    from ui.float_window import FloatWindow
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(paths, "get_resource_root", lambda: root)
    monkeypatch.setattr("ui.float_window.autostart.is_enabled", lambda: False)
    monkeypatch.setattr(FloatWindow, "_setup_tray",
                        lambda self: setattr(self, "tray_icon", SimpleNamespace(
                            hide=Mock(), setIcon=Mock(), setToolTip=Mock())))
    monkeypatch.setattr(FloatWindow, "_init_screen_watchers", lambda self: None)
    window = FloatWindow()
    try:
        window.startup_flow()
        window.show()
        qapp.processEvents()
        assert not window.account_store.list_accounts()
        assert window.config["update"]["enabled"] is False
        assert not window.grab().isNull()
    finally:
        window.poll_manager.stop()
        window.close()


def test_rate_limit_429_is_transient_and_keeps_last_good(qapp):
    """429 限流是瞬时错误:不覆盖上次成功值,界面不应闪红。"""
    provider = get_provider("deepseek")
    capture(provider._session, status=429, body={"error": "rate_limited"})
    with pytest.raises(TransientFetchError):
        provider.fetch({"api_key": "FAKE"})

    store = Mock()
    store.get_account.return_value = SimpleNamespace(enabled=True)
    manager = PollManager(store, 60_000)
    good = QuotaSnapshot.success_snapshot("deepseek", [], credential_message="ok")
    manager._on_one("a1", good)
    manager._on_one("a1", QuotaSnapshot.failure_snapshot(
        "deepseek", credential_status="valid", error_message="429", is_transient=True))
    assert manager.get_snapshot("a1") is good


def test_manual_refresh_restarts_timer(qapp):
    store = Mock()
    store.list_enabled.return_value = []
    manager = PollManager(store, 60_000)
    manager.start()
    manager._timer.start(1_000)          # 模拟快到期
    manager.refresh_now()
    assert manager._timer.remainingTime() > 50_000
    manager.stop()


def test_reorder_accounts_persists_and_rejects_stale_list(isolation):
    store = AccountStore()
    a = store.add_account("deepseek", "a", {"api_key": "FAKE"})
    b = store.add_account("deepseek", "b", {"api_key": "FAKE"})
    store.reorder_accounts([b.id, a.id])
    assert [x.id for x in AccountStore().list_accounts()] == [b.id, a.id]
    with pytest.raises(AccountStoreError):
        store.reorder_accounts([a.id])           # 缺账号(期间被删/新增)→ 拒绝
    assert [x.id for x in store.list_accounts()] == [b.id, a.id]


def _panel_with(accounts, qapp):
    from ui.detail_panel import DetailPanel
    panel = DetailPanel()
    panel.set_accounts(accounts, {"display_mode": "multi", "visible_account_ids": []})
    return panel


def test_panel_drag_emits_new_order(qapp):
    accs = [SimpleNamespace(id=i, label=i, provider_id="deepseek", enabled=True) for i in "abc"]
    panel = _panel_with(accs, qapp)
    got = []
    panel.accounts_reordered.connect(got.append)
    model = panel.account_list.model()
    assert model.moveRow(model.index(0, 0).parent(), 2, model.index(0, 0).parent(), 0)
    qapp.processEvents()
    assert got == [["c", "a", "b"]]


def test_panel_rename_edits_label_only(qapp):
    from ui.detail_panel import _LabelEditDelegate
    accs = [SimpleNamespace(id="a", label="工作号", provider_id="deepseek", enabled=True)]
    panel = _panel_with(accs, qapp)
    item = panel.account_list.item(0)
    assert item.flags() & Qt.ItemFlag.ItemIsEditable
    got = []
    panel.rename_requested.connect(lambda aid, name: got.append((aid, name)))
    delegate = panel.account_list.itemDelegate()
    assert isinstance(delegate, _LabelEditDelegate)
    index = panel.account_list.model().index(0, 0)
    editor = delegate.createEditor(panel.account_list, None, index)
    delegate.setEditorData(editor, index)
    assert editor.text() == "工作号"                # 不带"· 平台名"后缀
    editor.setText("  个人号 ")
    delegate.setModelData(editor, panel.account_list.model(), index)
    qapp.processEvents()
    assert got == [("a", "个人号")]
    assert "工作号" in item.text()                  # 列表项本身不被改,等落库后重建


def test_bar_order_follows_store_not_check_order():
    from ui.float_window import FloatWindow, MODE_MULTI
    window = SimpleNamespace(
        _enabled_account_ids=lambda: ["a", "b", "c"],
        ui_state={"display_mode": MODE_MULTI, "visible_account_ids": ["c", "a"]})
    assert FloatWindow._visible_account_ids(window) == ["a", "c"]


def _make_full_window(qapp, monkeypatch):
    from ui.float_window import FloatWindow
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(paths, "get_resource_root", lambda: root)
    monkeypatch.setattr("ui.float_window.autostart.is_enabled", lambda: False)
    monkeypatch.setattr(FloatWindow, "_setup_tray",
                        lambda self: setattr(self, "tray_icon", SimpleNamespace(hide=Mock())))
    monkeypatch.setattr(FloatWindow, "_init_screen_watchers", lambda self: None)
    window = FloatWindow()
    window.startup_flow()          # 无账号 → 展开在设置页
    window.show()
    window.move(100, 100)
    qapp.processEvents()
    return window


def test_user_resize_width_and_panel_height(qapp, monkeypatch):
    from utils.ui_state import load_ui_state
    window = _make_full_window(qapp, monkeypatch)
    try:
        # 设置页:只给左右手柄(高度随内容,不能拖)
        assert window._is_expanded and window.detail_panel.showing_settings
        assert not window._grips["bottom"].isVisible()

        # 右边拖宽 60
        window._begin_user_resize()
        window._user_resize("right", 60, 0)
        window._end_user_resize()
        assert window.width() == 320 and window.detail_panel.width() == 320
        assert load_ui_state()["window_width"] == 320

        # 左边拖窄 40:右边缘保持不动
        right = window.geometry().right()
        window._begin_user_resize()
        window._user_resize("left", 40, 0)
        window._end_user_resize()
        assert window.width() == 280 and window.geometry().right() == right

        # 切到列表页才有底边手柄;拖矮到下限,内容放不下时面板可滚动
        window.detail_panel.toggle_settings()
        qapp.processEvents()
        assert window._grips["bottom"].isVisible()
        window._begin_user_resize()
        window._user_resize("bottom", 0, -2000)
        window._end_user_resize()
        bar_h = window._current_bar_height()
        assert window.detail_panel.height() == window.MIN_PANEL_H
        assert window.height() == bar_h + window.MIN_PANEL_H
        assert load_ui_state()["panel_height"] == window.MIN_PANEL_H

        # 宽度有下限
        window._begin_user_resize()
        window._user_resize("right", -5000, 0)
        assert window.width() == window.MIN_WIDTH

        # 恢复默认
        window._reset_window_size()
        assert window.width() == window.config["window"]["width"]
        assert load_ui_state()["window_width"] is None
    finally:
        window.poll_manager.stop()
        window.close()


def test_panel_scrolls_only_when_content_taller(qapp):
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtCore import QPointF, QPoint
    from api.models import QuotaTier, KIND_PERCENT
    from ui.detail_panel import DetailPanel
    accs = [SimpleNamespace(id=i, label=i, provider_id="deepseek", enabled=True) for i in "abc"]
    panel = DetailPanel()
    panel.set_accounts(accs, {"display_mode": "multi", "visible_account_ids": []})
    tier = QuotaTier(name="five_hour", kind=KIND_PERCENT, utilization=10)
    panel.set_snapshots({i: QuotaSnapshot.success_snapshot("deepseek", [tier, tier]) for i in "abc"})
    panel.resize(300, panel.get_required_height())

    def wheel(dy):
        ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, dy),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        panel.wheelEvent(ev)

    wheel(-120)
    assert panel._scroll == 0                      # 放得下:不滚
    panel.resize(300, 150)
    for _ in range(50):
        wheel(-120)
    assert panel._scroll == panel._max_scroll() > 0  # 滚到底为止
    assert not panel.grab().isNull()


# ====== 高级设置 / 导入导出 ======
def test_settings_validation_rejects_bad_values():
    from utils.app_settings import validate_settings, SettingsError
    assert validate_settings({"refresh": {"interval_minutes": 3}}) == {"refresh": {"interval_minutes": 3}}
    for bad in [{"refresh": {"interval_minutes": 0}}, {"refresh": {"interval_minutes": True}},
                {"refresh": {"interval_minutes": "5"}}, {"update": {"enabled": 1}},
                {"window": {"width": 300}}, {"refresh": {"foo": 1}}, []]:
        with pytest.raises(SettingsError):
            validate_settings(bad)


def test_user_settings_override_and_corrupt_file_ignored():
    from utils import app_settings
    app_settings.save_user_settings({"refresh": {"interval_minutes": 7}})
    assert app_settings.load_user_settings() == {"refresh": {"interval_minutes": 7}}
    app_settings.user_settings_path().write_text("[refresh\nbroken", encoding="utf-8")
    assert app_settings.load_user_settings() == {}


def test_export_import_roundtrip_without_secrets(tmp_path):
    from utils import app_settings
    from utils.proxy_config import ProxySettings
    out = tmp_path / "exp.toml"
    overrides = {"refresh": {"interval_minutes": 9}, "update": {"enabled": False, "check_on_startup": False}}
    app_settings.export_settings(str(out), overrides, ProxySettings("custom", "http://127.0.0.1:7890"))
    text = out.read_text(encoding="utf-8")
    assert "api_key" not in text.lower() and "accounts" not in text
    got, proxy = app_settings.import_settings(str(out))
    assert got == overrides and proxy == ProxySettings("custom", "http://127.0.0.1:7890")


@pytest.mark.parametrize("content", [
    "[refresh]\ninterval_minutes = 0\n",
    "[proxy]\nmode = \"custom\"\nurl = \"http://user:pw@host:1\"\n",
    "[accounts]\nx = 1\n",
    "not toml [",
])
def test_import_rejects_invalid_file(tmp_path, content):
    from utils import app_settings
    path = tmp_path / "bad.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(app_settings.SettingsError):
        app_settings.import_settings(str(path))


def test_proxy_saved_from_ui_applies_immediately():
    from utils import proxy_config
    from utils.proxy_config import ProxySettings, ProxyConfigError, save_and_apply_proxy
    before = proxy_config.get_proxy_settings()
    try:
        save_and_apply_proxy(ProxySettings("custom", "http://127.0.0.1:7890"))
        assert ProviderSession("deepseek").proxies == {"http": "http://127.0.0.1:7890",
                                                       "https": "http://127.0.0.1:7890"}
        path = Path(paths.get_data_path("proxy.toml"))
        assert proxy_config.load_proxy_config(path) == ProxySettings("custom", "http://127.0.0.1:7890")
        with pytest.raises(ProxyConfigError):
            save_and_apply_proxy(ProxySettings("custom", "socks5://127.0.0.1:1"))
        assert proxy_config.get_proxy_settings().url == "http://127.0.0.1:7890"   # 非法不替换
    finally:
        proxy_config._config = before


def test_apply_settings_saves_and_takes_effect(qapp, monkeypatch):
    from utils import app_settings
    from utils.app_settings import SettingsError
    window = _make_full_window(qapp, monkeypatch)
    window._rebuild_tray_menu = Mock()
    try:
        window.apply_settings({"refresh": {"interval_minutes": 2}})
        assert window.poll_manager._interval_ms == 120_000
        assert app_settings.load_user_settings()["refresh"] == {"interval_minutes": 2}
        window.apply_settings({"update": {"enabled": True}})
        window._rebuild_tray_menu.assert_called_once()
        with pytest.raises(SettingsError):
            window.apply_settings({"refresh": {"interval_minutes": 999}})
        assert window.config["refresh"]["interval_minutes"] == 2           # 非法不改
        assert window._load_config()["refresh"]["interval_minutes"] == 2   # 重启沿用
    finally:
        window.poll_manager.stop()
        window.close()


def test_settings_dialog_import_applies_everything(qapp, monkeypatch, tmp_path):
    from ui.settings_dialog import SettingsDialog
    from utils import proxy_config
    window = _make_full_window(qapp, monkeypatch)
    window._rebuild_tray_menu = Mock()
    before = proxy_config.get_proxy_settings()
    try:
        path = tmp_path / "in.toml"
        path.write_text("[refresh]\ninterval_minutes = 15\n[proxy]\nmode = \"direct\"\nurl = \"\"\n",
                        encoding="utf-8")
        dlg = SettingsDialog(window)
        assert dlg.import_from(str(path))
        assert window.config["refresh"]["interval_minutes"] == 15
        assert dlg.interval_spin.value() == 15
        assert proxy_config.get_proxy_settings().mode == "direct"
        path.write_text("[refresh]\ninterval_minutes = 500\n", encoding="utf-8")
        assert not dlg.import_from(str(path))
        assert window.config["refresh"]["interval_minutes"] == 15
    finally:
        proxy_config._config = before
        window.poll_manager.stop()
        window.close()


# ====== 看板拖拽排序 ======
def _dashboard(qapp, ids, visible=None):
    from api.models import QuotaTier, KIND_PERCENT
    from ui.detail_panel import DetailPanel
    accs = [SimpleNamespace(id=i, label=i, provider_id="deepseek", enabled=True) for i in ids]
    panel = DetailPanel()
    panel.set_accounts(accs, {"display_mode": "multi", "visible_account_ids": visible or []})
    tier = QuotaTier(name="five_hour", kind=KIND_PERCENT, utilization=10)
    panel.set_snapshots({i: QuotaSnapshot.success_snapshot("deepseek", [tier]) for i in ids})
    panel.resize(300, panel.get_required_height())
    return panel


def _drag(panel, from_y, to_y):
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QPoint
    QTest.mousePress(panel, Qt.MouseButton.LeftButton, pos=QPoint(100, from_y))
    QTest.mouseMove(panel, QPoint(100, to_y))
    QTest.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=QPoint(100, to_y))


def test_dashboard_drag_moves_block_down(qapp):
    panel = _dashboard(qapp, "abc")
    got = []
    panel.accounts_reordered.connect(got.append)
    spans = panel._block_spans()
    _drag(panel, spans[0][1] + 10, spans[2][2] - 5)     # a 拖到最后
    assert got == [["b", "c", "a"]]


def test_dashboard_drag_keeps_hidden_accounts_in_place(qapp):
    panel = _dashboard(qapp, "abcd", visible=["a", "c", "d"])   # b 未勾选
    got = []
    panel.accounts_reordered.connect(got.append)
    spans = panel._block_spans()                       # 显示 a c d
    _drag(panel, spans[2][1] + 10, spans[0][1] + 2)    # d 拖到最前
    assert got == [["d", "b", "a", "c"]]


def test_dashboard_click_without_move_does_not_reorder(qapp):
    panel = _dashboard(qapp, "ab")
    got = []
    panel.accounts_reordered.connect(got.append)
    y = panel._block_spans()[0][1] + 10
    _drag(panel, y, y + 2)
    assert got == []


def test_account_refresh_icon_refreshes_only_that_account(qapp):
    import time
    from datetime import datetime
    panel = _dashboard(qapp, "abc")
    panel.grab()                                       # 绘制一次,生成图标位置
    got = []
    panel.account_refresh_clicked.connect(got.append)
    x, y, w, h = panel._acc_refresh_rects["b"]
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QPoint
    QTest.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(x + w // 2, y + h // 2))
    assert got == ["b"]
    panel.set_account_refreshing("b")                 # 刷新中再点无效
    QTest.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(x + w // 2, y + h // 2))
    assert got == ["b"]
    snap = QuotaSnapshot.success_snapshot("deepseek", [])
    snap.queried_at = int(datetime.now().replace(hour=9, minute=5).timestamp() * 1000)
    assert panel._data_time_text(snap) == "09:05"
    snap.queried_at = int(datetime(2026, 1, 2, 3, 4).timestamp() * 1000)
    assert panel._data_time_text(snap) == "01-02 03:04"


def test_poll_refresh_account_fetches_single_account(qapp, monkeypatch):
    accounts = {i: SimpleNamespace(id=i, provider_id="deepseek", enabled=True,
                                   base_url_override=None) for i in "ab"}
    store = Mock()
    store.get_account.side_effect = accounts.get
    store.list_enabled.return_value = list(accounts.values())
    manager = PollManager(store, 60_000)
    started = []
    monkeypatch.setattr(manager, "_start_fetch", lambda acc: (started.append(acc.id),
                                                              manager._inflight.add(acc.id)))
    assert manager.refresh_account("a")
    assert started == ["a"]
    assert not manager.refresh_account("a")            # 采集中不重复发起
    assert not manager.refresh_account("zzz")
    manager._tick()                                     # 自动轮询跳过采集中的 a
    assert started == ["a", "b"]


def test_refresh_icon_spins_while_refreshing(qapp):
    panel = _dashboard(qapp, "ab")
    assert not panel._spin_timer.isActive()
    panel.set_account_refreshing("a")
    assert panel._spin_timer.isActive()
    before = panel._spin_angle
    panel._advance_spin()
    assert panel._spin_angle != before
    assert not panel.grab().isNull()            # 旋转绘制不报错
    panel.clear_refreshing()
    assert not panel._spin_timer.isActive()     # 刷新完停止动画,不空转


# ====== 外观:背景色 / 背景图 ======
def test_appearance_validation():
    from utils.app_settings import validate_settings, SettingsError
    ok = {"appearance": {"bg_color": "#1A2b3C", "bg_opacity": 0, "bg_image": "background.png"}}
    assert validate_settings(ok) == ok
    for bad in [{"appearance": {"bg_color": "red"}}, {"appearance": {"bg_opacity": 101}},
                {"appearance": {"bg_image": "../evil.png"}},
                {"appearance": {"bg_image": "C:/x/background.png"}}]:
        with pytest.raises(SettingsError):
            validate_settings(bad)


def test_background_image_stored_in_data_and_applied(qapp, monkeypatch, tmp_path):
    from PySide6.QtGui import QImage
    from ui.settings_dialog import SettingsDialog
    from utils import app_settings
    src = tmp_path / "pic.jpg"
    img = QImage(40, 20, QImage.Format.Format_RGB32)
    img.fill(QColor(200, 30, 30))
    assert img.save(str(src))
    window = _make_full_window(qapp, monkeypatch)
    try:
        dlg = SettingsDialog(window)
        assert dlg.set_background_image(str(src))
        assert window.config["appearance"]["bg_image"] == "background.jpg"
        assert Path(paths.get_data_path("background.jpg")).exists()
        assert window._bg_pixmap is not None
        assert app_settings.load_user_settings()["appearance"]["bg_image"] == "background.jpg"
        assert not window.grab().isNull()
        window.apply_settings({"appearance": {"bg_color": "#336699", "bg_opacity": 40}})
        assert window._bg_color.name() == "#336699" and window._bg_color.alpha() == 102
        dlg._on_clear_image()
        assert window._bg_pixmap is None
        assert not Path(paths.get_data_path("background.jpg")).exists()
        bad = tmp_path / "x.png"
        bad.write_bytes(b"not an image")
        assert not dlg.set_background_image(str(bad))
    finally:
        window.poll_manager.stop()
        window.close()
