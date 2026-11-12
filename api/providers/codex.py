"""Codex(ChatGPT 官方订阅)—— 读 ~/.codex/auth.json 的 OAuth token。

【凭据】只认 auth_mode=="chatgpt" 的(API-key 模式不算):
  {auth_mode:"chatgpt", tokens:{access_token, account_id}, last_refresh}
【接口】GET chatgpt.com/backend-api/wham/usage,User-Agent: codex-cli,
  可选 ChatGPT-Account-Id。
【解析】rate_limit.primary_window/secondary_window {used_percent, limit_window_seconds, reset_at(秒)}。
  limit_window_seconds → 档位:18000=five_hour,604800=seven_day,2592000=30_day。
"""
import logging
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_PERCENT, QuotaSnapshot, TIER_FIVE_HOUR, TIER_SEVEN_DAY, TIER_THIRTY_DAY
from api.provider_base import QuotaProvider, ProviderError, CATEGORY_OAUTH_CLI
from api.registry import register
from api._http import get_json, TransientFetchError, DeterministicError
from api.time_utils import format_reset_time_seconds

logger = logging.getLogger(__name__)


@register
class CodexProvider(QuotaProvider):
    provider_id = "codex"
    display_name = "Codex(ChatGPT)"
    category = CATEGORY_OAUTH_CLI
    credential_schema = []

    def _read_token(self) -> Tuple[Optional[str], Optional[str]]:
        data = QuotaProvider.read_cli_json(".codex", "auth.json")
        if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt":
            return None, None
        tokens = data.get("tokens") or {}
        return tokens.get("access_token"), tokens.get("account_id")

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        token, account_id = self._read_token()
        if not token:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="not_found",
                error_message="未找到 ~/.codex/auth.json 的 ChatGPT 登录(请先用 Codex CLI 登录)")
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        try:
            body, _status = get_json(self._session,
                                     "https://chatgpt.com/backend-api/wham/usage", headers)
        except TransientFetchError:
            raise
        except DeterministicError as e:
            return self._failure_from_deterministic(e)
        try:
            return self._parse(body)
        except ProviderError as e:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message=str(e))
        except Exception as e:
            logger.warning("[codex] 解析失败(%s)", type(e).__name__)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message="额度响应解析失败")

    @staticmethod
    def _window_name(secs) -> str:
        if secs == 18000:
            return TIER_FIVE_HOUR
        if secs == 604800:
            return TIER_SEVEN_DAY
        if secs == 2592000:
            return TIER_THIRTY_DAY
        if isinstance(secs, (int, float)):
            if secs >= 86400:
                return f"{int(secs // 86400)}_day"
            return f"{int(secs // 3600)}_hour"
        return "unknown"

    def _parse(self, body: dict) -> QuotaSnapshot:
        rl = body.get("rate_limit") or {}
        tiers: List[QuotaTier] = []
        for wkey in ("primary_window", "secondary_window"):
            w = rl.get(wkey)
            if not isinstance(w, dict):
                continue
            used = w.get("used_percent")
            if used is None:
                continue
            tiers.append(QuotaTier(
                name=self._window_name(w.get("limit_window_seconds")),
                kind=KIND_PERCENT, utilization=float(used),
                resets_at=format_reset_time_seconds(w.get("reset_at"))))
        if not tiers:
            raise ProviderError("未解析到 Codex 用量窗口")
        return QuotaSnapshot.success_snapshot(self.provider_id, tiers, credential_message="ChatGPT 订阅")
