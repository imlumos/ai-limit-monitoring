"""统一代理配置回归。网络请求在 Adapter 层拦截,不连接真实代理。"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.transport import ALLOWED_HOSTS, ProviderSession
from utils import paths, proxy_config
from utils.paths import get_project_root
from utils.proxy_config import ProxyConfigError, ProxySettings, load_proxy_config


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy_config, "_config", ProxySettings())
    monkeypatch.setattr(paths, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))
    calls = []
    def send(adapter, request, **kwargs):
        calls.append((request, kwargs))
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        response._content = b"{}"
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls


def write_config(tmp_path, mode="custom", url="http://127.0.0.1:19876"):
    path = tmp_path / "data" / "proxy.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"[proxy]\nmode = {json.dumps(mode)}\nurl = {json.dumps(url)}\n", encoding="utf-8")
    return path


def test_first_launch_creates_template_without_changing_existing_file(tmp_path):
    path = tmp_path / "data" / "proxy.toml"
    assert load_proxy_config(path) == ProxySettings()
    assert path.read_text(encoding="utf-8") == proxy_config.TEMPLATE
    path = write_config(tmp_path)
    before = path.read_bytes()
    assert load_proxy_config(path) == ProxySettings("custom", "http://127.0.0.1:19876")
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider_id,host", [
    (pid, host) for pid, hosts in ALLOWED_HOSTS.items() for host in sorted(hosts)
])
def test_every_platform_uses_one_configured_proxy(tmp_path, monkeypatch, isolated_config, provider_id, host):
    write_config(tmp_path)
    proxy_config.initialize_proxy_config()
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.invalid:1234")
    monkeypatch.setenv("ALL_PROXY", "http://environment.invalid:1234")
    monkeypatch.setenv("NO_PROXY", "*")
    session = ProviderSession(provider_id)
    session.get(f"https://{host}/")
    options = isolated_config[-1][1]
    assert session.trust_env is False
    assert options["proxies"] == {"http": "http://127.0.0.1:19876", "https": "http://127.0.0.1:19876"}
    assert options["verify"] is True


def test_direct_mode_ignores_environment_proxy(tmp_path, monkeypatch, isolated_config):
    write_config(tmp_path, "direct", "")
    proxy_config.initialize_proxy_config()
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.invalid:1234")
    session = ProviderSession("codex")
    session.get("https://chatgpt.com/backend-api/wham/usage")
    assert session.trust_env is False
    assert not isolated_config[-1][1]["proxies"]


def test_environment_mode_keeps_existing_behavior(tmp_path, monkeypatch, isolated_config):
    write_config(tmp_path, "environment", "")
    proxy_config.initialize_proxy_config()
    for name in ["NO_PROXY", "no_proxy", "https_proxy"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.invalid:1234")
    session = ProviderSession("claude")
    session.get("https://api.anthropic.com/api/oauth/usage")
    assert session.trust_env is True
    assert isolated_config[-1][1]["proxies"]["https"] == "http://environment.invalid:1234"


@pytest.mark.parametrize("url", [
    "", "127.0.0.1:7890", "socks5://127.0.0.1:7890", "file:///tmp/proxy",
    "http://user:FAKE_SECRET@localhost:7890", "http://localhost:7890/?token=FAKE_SECRET",
    "http://localhost:7890/path", "http://localhost:7890/#FAKE_SECRET",
    "http://localhost:0", "http://localhost:65536", "http://[bad", "http://local host:7890",
])
def test_invalid_proxy_rejected_without_echoing_contents(tmp_path, url):
    path = write_config(tmp_path, url=url)
    with pytest.raises(ProxyConfigError) as error:
        load_proxy_config(path)
    assert "FAKE_SECRET" not in str(error.value)


@pytest.mark.parametrize("mode,url", [("unknown", ""), (False, ""), ("custom", 123), ("direct", "http://localhost:7890")])
def test_invalid_options_are_not_silently_ignored(tmp_path, mode, url):
    with pytest.raises(ProxyConfigError):
        load_proxy_config(write_config(tmp_path, mode, url))


def test_no_per_platform_configuration_in_first_version(tmp_path):
    path = write_config(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write('\n[providers.claude]\nmode="direct"\n')
    with pytest.raises(ProxyConfigError):
        load_proxy_config(path)


def test_invalid_toml_not_overwritten_or_echoed(tmp_path):
    path = write_config(tmp_path)
    path.write_text('[proxy]\nurl = "FAKE_SECRET', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ProxyConfigError) as error:
        load_proxy_config(path)
    assert "FAKE_SECRET" not in str(error.value)
    assert path.read_bytes() == before


def test_windows_utf8_bom_supported(tmp_path):
    path = write_config(tmp_path)
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert load_proxy_config(path).mode == "custom"


def test_exe_directory_used_instead_of_bundled_resource_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_project_root", get_project_root)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    proxy_config.initialize_proxy_config()
    assert (tmp_path / "data/proxy.toml").exists()
    assert not (tmp_path / "_internal/data/proxy.toml").exists()


def test_changed_configuration_requires_restart_reload(tmp_path):
    write_config(tmp_path)
    proxy_config.initialize_proxy_config()
    write_config(tmp_path, url="http://127.0.0.1:19877")
    assert ProviderSession("codex").proxies["https"] == "http://127.0.0.1:19876"
    proxy_config.initialize_proxy_config()
    assert ProviderSession("codex").proxies["https"] == "http://127.0.0.1:19877"


def test_proxy_failure_does_not_retry_direct(tmp_path, monkeypatch):
    write_config(tmp_path)
    proxy_config.initialize_proxy_config()
    send = Mock(side_effect=requests.exceptions.ProxyError("proxy unavailable"))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    session = ProviderSession("codex")
    with pytest.raises(requests.exceptions.ProxyError):
        session.get("https://chatgpt.com/backend-api/wham/usage")
    assert send.call_count == 1
    assert send.call_args.kwargs["proxies"]["https"] == "http://127.0.0.1:19876"


def test_bad_configuration_stops_startup_before_account_or_network_access(tmp_path, monkeypatch):
    import main
    path = write_config(tmp_path)
    path.write_text("broken TOML [FAKE_SECRET", encoding="utf-8")
    monkeypatch.setattr(main, "setup_logging", Mock())
    monkeypatch.setattr(main, "QApplication", Mock())
    warning = Mock()
    monkeypatch.setattr(main, "QMessageBox", SimpleNamespace(critical=warning))
    window = Mock()
    monkeypatch.setattr(main, "FloatWindow", window)
    main.main()
    warning.assert_called_once()
    window.assert_not_called()
    assert "FAKE_SECRET" not in str(warning.call_args)
