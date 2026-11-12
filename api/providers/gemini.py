"""Gemini(Google 官方)—— 读 ~/.gemini/oauth_creds.json,需要刷新 token + 两步 POST。

【凭据】{access_token, refresh_token, expiry_date(毫秒)}。expiry 过期就用 refresh_token 换新的。
【刷新】POST oauth2.googleapis.com/token(form 表单),客户端参数读取本地 data/gemini-oauth.toml。
【两步】
  1) POST cloudcode-pa.googleapis.com/v1internal:loadCodeAssist → cloudaicompanionProject.id
  2) POST cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota {project:id} → buckets[]
【解析】按 modelId 分组(gemini_pro/flash/flash_lite),每组取最小 remainingFraction,
  utilization=(1-remaining)*100。
"""
import logging
import time
import tomllib
from typing import List, Optional, Dict

from api.models import (QuotaTier, KIND_PERCENT, QuotaSnapshot,
                        TIER_THIRTY_DAY)
from api.provider_base import QuotaProvider, ProviderError, CATEGORY_OAUTH_CLI
from api.registry import register
from api._http import post_json, TransientFetchError, DeterministicError
from api.time_utils import format_reset_time_iso
from utils.paths import get_data_path
import requests

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_LOAD_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"

_CATEGORY_ORDER = {"gemini_pro": 0, "gemini_flash": 1, "gemini_flash_lite": 2}


def _read_oauth_client() -> dict:
    """读取不随源码分发的客户端参数,错误中不带文件内容。"""
    message = "请配置 data/gemini-oauth.toml,或先在 Gemini CLI 重新登录"
    try:
        with open(get_data_path("gemini-oauth.toml"), "rb") as source:
            content = source.read(16 * 1024 + 1)
        if len(content) > 16 * 1024:
            raise ValueError
        raw = tomllib.loads(content.decode("utf-8-sig"))
        client = raw.get("oauth")
        if (set(raw) != {"oauth"} or not isinstance(client, dict)
                or set(client) != {"client_id", "client_secret"}
                or any(not isinstance(v, str) or not v.strip()
                       or any(ord(c) < 32 or ord(c) == 127 for c in v)
                       for v in client.values())):
            raise ValueError
    except (OSError, ValueError):
        raise ProviderError(message, credential_status="config_error") from None
    return {key: value.strip() for key, value in client.items()}


def _classify(model_id: str) -> str:
    mid = model_id or "unknown"
    if "flash-lite" in mid:
        return "gemini_flash_lite"
    if "flash" in mid:
        return "gemini_flash"
    if "pro" in mid:
        return "gemini_pro"
    return mid


@register
class GeminiProvider(QuotaProvider):
    provider_id = "gemini"
    display_name = "Gemini(官方)"
    category = CATEGORY_OAUTH_CLI
    credential_schema = []

    def _read_creds(self):
        data = QuotaProvider.read_cli_json(".gemini", "oauth_creds.json")
        if not isinstance(data, dict):
            return None, None, None
        return data.get("access_token"), data.get("refresh_token"), data.get("expiry_date")

    def _refresh(self, refresh_token: str) -> Optional[str]:
        """用 refresh_token 换新 access_token;失败返回 None(回退旧 token)。"""
        form = {
            **_read_oauth_client(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            resp = self._session.post(_TOKEN_URL, data=form, timeout=15,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise TransientFetchError("Gemini token 刷新网络错误，请检查代理和网络") from None
        if resp.status_code != 200:
            logger.warning("[gemini] token 刷新失败 HTTP %s", resp.status_code)
            return None
        try:
            return resp.json().get("access_token")
        except Exception:
            return None

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        access, refresh, expiry = self._read_creds()
        token = access
        now_ms = int(time.time() * 1000)
        # 过期(或没有 expiry)且有 refresh_token → 尝试刷新
        expired = (not isinstance(expiry, (int, float))) or expiry < now_ms
        if expired and refresh:
            try:
                new = self._refresh(refresh)
            except ProviderError as e:
                return QuotaSnapshot.failure_snapshot(
                    self.provider_id, credential_status=e.credential_status, error_message=str(e))
            if new:
                token = new
        if not token:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="not_found",
                error_message="未找到 ~/.gemini/oauth_creds.json(请先用 gemini CLI 登录)")

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 1) loadCodeAssist → project id
        try:
            b1, _ = post_json(self._session, _LOAD_URL, headers,
                              body={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}})
        except TransientFetchError:
            raise
        except DeterministicError as e:
            return self._failure_from_deterministic(e)
        proj = ((b1.get("cloudaicompanionProject") or {}).get("id")
                or (b1.get("cloudaicompanion_project") or {}).get("id"))

        # 2) retrieveUserQuota → buckets
        try:
            b2, _ = post_json(self._session, _QUOTA_URL, headers,
                              body={"project": proj} if proj else {})
        except TransientFetchError:
            raise
        except DeterministicError as e:
            return self._failure_from_deterministic(e)

        try:
            return self._parse(b2)
        except ProviderError as e:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message=str(e))
        except Exception as e:
            logger.warning("[gemini] 解析失败(%s)", type(e).__name__)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message="额度响应解析失败")

    def _parse(self, body: dict) -> QuotaSnapshot:
        buckets = body.get("buckets") or []
        groups: Dict[str, dict] = {}  # category -> {rem, reset}
        for b in buckets:
            if not isinstance(b, dict):
                continue
            cat = _classify(b.get("modelId"))
            rem = b.get("remainingFraction")
            rem = max(0.0, min(1.0, float(rem))) if isinstance(rem, (int, float)) else 1.0
            rt = b.get("resetTime")
            g = groups.get(cat)
            if g is None:
                groups[cat] = {"rem": rem, "reset": rt}
            elif rem < g["rem"]:
                g["rem"] = rem
                if rt:
                    g["reset"] = rt

        tiers: List[QuotaTier] = []
        for cat in sorted(groups, key=lambda c: _CATEGORY_ORDER.get(c, 3)):
            g = groups[cat]
            tiers.append(QuotaTier(
                name=cat, kind=KIND_PERCENT,
                utilization=round((1.0 - g["rem"]) * 100.0, 1),
                resets_at=format_reset_time_iso(g["reset"])))
        if not tiers:
            raise ProviderError("Gemini 未返回用量 buckets")
        return QuotaSnapshot.success_snapshot(self.provider_id, tiers, credential_message="Gemini CLI")
