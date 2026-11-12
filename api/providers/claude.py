"""Claude(Anthropic 官方订阅)—— 读 ~/.claude/.credentials.json 的 OAuth token。

【凭据】不收 API Key,而是读 Claude Code CLI 登录后写的 .credentials.json:
  {claudeAiOauth:{accessToken, expiresAt}} 或 {claude.ai_oauth:{...}}
【接口】GET api.anthropic.com/api/oauth/usage,头 anthropic-beta: oauth-2025-04-20。
【不刷新 token】只读 CLI 写的 token,过期由接口 401 判定。
  expiresAt(秒/毫秒/ISO 兼容)只是预检:本地判过期仍会"试一把"(时钟偏差时
  token 可能仍有效),真失败才报"已过期,请重新登录"——与 cc-switch 同策略。
"""
import logging
import time
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_PERCENT, QuotaSnapshot, TIER_FIVE_HOUR, TIER_SEVEN_DAY
from api.provider_base import QuotaProvider, ProviderError, CATEGORY_OAUTH_CLI
from api.registry import register
from api._http import get_json, TransientFetchError, DeterministicError
from api.time_utils import format_reset_time_iso

logger = logging.getLogger(__name__)

_KNOWN_TIERS = [TIER_FIVE_HOUR, TIER_SEVEN_DAY, "seven_day_opus", "seven_day_sonnet"]


def _is_token_expired(expires_at) -> bool:
    """expiresAt 兼容秒/毫秒时间戳与 ISO 字符串;无法解析不视为过期(与 cc-switch 一致)。

    负数/非法值不当过期——上游 Rust 的 as_u64() 对负数返回 None → false,
    这里对齐(否则 -1 之类哨兵值会被判"已过期")。
    """
    if isinstance(expires_at, bool) or expires_at is None:
        return False
    now = time.time()
    if isinstance(expires_at, (int, float)):
        if expires_at <= 0:
            return False
        ts = expires_at / 1000 if expires_at > 1_000_000_000_000 else expires_at
        return ts < now
    if isinstance(expires_at, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() < now
        except ValueError:
            return False
    return False


@register
class ClaudeProvider(QuotaProvider):
    provider_id = "claude"
    display_name = "Claude(官方订阅)"
    category = CATEGORY_OAUTH_CLI
    credential_schema = []  # 读本地 CLI 凭据,无需用户输入

    def _read_token(self) -> Tuple[Optional[str], bool]:
        """返回 (token, 本地判过期)。过期仍返回 token——调用方"试一把"。"""
        data = QuotaProvider.read_cli_json(".claude", ".credentials.json")
        if not isinstance(data, dict):
            return None, False
        oauth = data.get("claudeAiOauth") or data.get("claude.ai_oauth")
        if isinstance(oauth, dict):
            expired = _is_token_expired(oauth.get("expiresAt"))
            if expired:
                logger.info("[claude] token 本地判定已过期,仍尝试调用(时钟偏差可能仍有效)")
            return oauth.get("accessToken"), expired
        return None, False

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        token, local_expired = self._read_token()
        if not token:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="not_found",
                error_message="未找到 ~/.claude/.credentials.json(请先用 Claude Code 登录)")
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        }
        try:
            body, _status = get_json(self._session,
                                     "https://api.anthropic.com/api/oauth/usage", headers)
        except TransientFetchError:
            raise
        except DeterministicError as e:
            snap = self._failure_from_deterministic(e)
            # 本地预检已判过期 + 接口也鉴权失败 → 给"重新登录"的明确文案
            if local_expired and snap.credential_status == "expired":
                snap = QuotaSnapshot.failure_snapshot(
                    self.provider_id, credential_status="expired",
                    error_message="OAuth token 已过期,请重新用 Claude Code 登录")
            return snap
        try:
            return self._parse(body)
        except ProviderError as e:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message=str(e))
        except Exception as e:
            logger.warning("[claude] 解析失败(%s)", type(e).__name__)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message="额度响应解析失败")

    def _parse(self, body: dict) -> QuotaSnapshot:
        tiers: List[QuotaTier] = []
        # 已知窗口按固定顺序
        for name in _KNOWN_TIERS:
            win = body.get(name)
            if isinstance(win, dict) and win.get("utilization") is not None:
                tiers.append(QuotaTier(name=name, kind=KIND_PERCENT,
                                       utilization=float(win["utilization"]),
                                       resets_at=format_reset_time_iso(win.get("resets_at"))))
        # 其余顶层窗口(extra_usage 与已知窗口除外)
        for k, v in body.items():
            if k in ("extra_usage",) or k in _KNOWN_TIERS:
                continue
            if isinstance(v, dict) and v.get("utilization") is not None:
                tiers.append(QuotaTier(name=k, kind=KIND_PERCENT,
                                       utilization=float(v["utilization"]),
                                       resets_at=format_reset_time_iso(v.get("resets_at"))))
        if not tiers:
            raise ProviderError("未解析到任何 Claude 用量窗口")
        return QuotaSnapshot.success_snapshot(self.provider_id, tiers, credential_message="Claude Pro/Max")
