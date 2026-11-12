"""Gemini OAuth 客户端本地配置及缺失配置时的请求边界。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from api.providers import gemini
from api.provider_base import QuotaProvider
from utils import paths, proxy_config
from utils.proxy_config import ProxySettings


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(proxy_config, "_config", ProxySettings(mode="direct"))
    monkeypatch.setattr(QuotaProvider, "read_cli_json", staticmethod(lambda *parts: {
        "access_token": "FAKE_OLD_ACCESS", "refresh_token": "FAKE_REFRESH", "expiry_date": 1,
    }))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", Mock(
        side_effect=AssertionError("Real network access is forbidden")))
    instance = gemini.GeminiProvider()
    monkeypatch.setattr(instance._session, "post", Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {"access_token": "FAKE_NEW_ACCESS"})))
    monkeypatch.setattr(gemini, "post_json", Mock(side_effect=[
        ({"cloudaicompanionProject": {"id": "FAKE_PROJECT"}}, 200),
        ({"buckets": [{"modelId": "gemini-pro", "remainingFraction": 0.8}]}, 200),
    ]))
    try:
        yield instance
    finally:
        instance.close()


def write_config(tmp_path, content):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "gemini-oauth.toml").write_bytes(content)


def test_missing_client_config_stops_before_network(provider):
    snapshot = provider.fetch({})
    assert not snapshot.success
    assert snapshot.credential_status == "config_error"
    assert "data/gemini-oauth.toml" in snapshot.error_message
    provider._session.post.assert_not_called()
    gemini.post_json.assert_not_called()


@pytest.mark.parametrize("content", [
    b"not valid TOML: FAKE_SECRET",
    b"[oauth]\nclient_id='FAKE_ID'",
    b"[oauth]\nclient_id='FAKE_ID'\nclient_secret=''",
    b"[oauth]\nclient_id=123\nclient_secret='FAKE_SECRET'",
    b"[oauth]\nclient_id='FAKE_ID'\nclient_secret='FAKE_SECRET'\nextra=true",
    b"[oauth]\nclient_id='FAKE_ID'\nclient_secret='FAKE_SECRET'\n[unknown]",
    b'[oauth]\nclient_id="FAKE_ID"\nclient_secret="FAKE_SECRET\\nINJECTED"',
    b"\xffFAKE_SECRET",
    b"# FAKE_SECRET" + b"x" * (16 * 1024),
])
def test_invalid_client_config_is_private_and_sends_nothing(provider, tmp_path, content):
    write_config(tmp_path, content)
    snapshot = provider.fetch({})
    assert snapshot.credential_status == "config_error"
    assert "FAKE_SECRET" not in snapshot.error_message
    provider._session.post.assert_not_called()
    gemini.post_json.assert_not_called()


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_local_client_config_used_only_for_google_refresh(provider, tmp_path, bom):
    write_config(tmp_path, bom + (
        b"[oauth]\nclient_id='FAKE_CLIENT_ID'\nclient_secret='FAKE_CLIENT_SECRET'"))
    snapshot = provider.fetch({})
    assert snapshot.success
    args, kwargs = provider._session.post.call_args
    assert args == ("https://oauth2.googleapis.com/token",)
    assert kwargs["data"] == {
        "client_id": "FAKE_CLIENT_ID", "client_secret": "FAKE_CLIENT_SECRET",
        "refresh_token": "FAKE_REFRESH", "grant_type": "refresh_token",
    }
    for call in gemini.post_json.call_args_list:
        assert call.args[2]["Authorization"] == "Bearer FAKE_NEW_ACCESS"


def test_unexpired_access_token_needs_no_client_config(provider, monkeypatch):
    monkeypatch.setattr(provider, "_read_creds", lambda: (
        "FAKE_VALID_ACCESS", "FAKE_REFRESH", 9999999999999))
    assert provider.fetch({}).success
    provider._session.post.assert_not_called()
    assert gemini.post_json.call_args_list[0].args[2]["Authorization"] == "Bearer FAKE_VALID_ACCESS"
