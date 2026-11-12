"""ZenMux —— Bearer key,额度接口地址由用户填(base_url 即接口本身)。

响应:{success, message, data:{quota_5_hour:{usage_percentage(0~1),resets_at,used_value_usd,max_value_usd},
                              quota_7_day:{...}, plan:{tier}, account_status}}
  usage_percentage 是 0~1 的小数 → ×100。tier 等级 + 账号状态作为备注。
"""
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_PERCENT, TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT
from api.provider_base import BearerKeyProvider, CredentialField
from api.registry import register
from api.time_utils import format_reset_time_iso


@register
class ZenMuxProvider(BearerKeyProvider):
    provider_id = "zenmux"
    display_name = "ZenMux"
    # base_url 是必填的(它本身就是额度接口地址)
    credential_schema = [
        CredentialField("api_key", "API Key", secret=True),
        CredentialField("base_url", "额度接口地址", secret=False, required=True,
                        placeholder="https://your-zenmux/usage"),
    ]

    def _url(self, base_url, credentials):
        # 用户填的 base_url 就是接口地址
        return credentials.get("base_url") or base_url

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        data = body.get("data") or {}
        tiers: List[QuotaTier] = []

        for key, tier_name in (("quota_5_hour", TIER_FIVE_HOUR),
                               ("quota_7_day", TIER_WEEKLY_LIMIT)):
            q = data.get(key) or {}
            if not q:
                continue
            pct = q.get("usage_percentage")
            tiers.append(QuotaTier(
                name=tier_name, kind=KIND_PERCENT,
                utilization=round(pct * 100, 1) if isinstance(pct, (int, float)) else None,
                resets_at=format_reset_time_iso(q.get("resets_at"))))

        plan = (data.get("plan") or {}).get("tier")
        acct = data.get("account_status")
        message = f"{plan} ({acct})" if (plan and acct) else (plan or acct)
        return tiers, message
