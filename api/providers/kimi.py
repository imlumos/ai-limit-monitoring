"""Kimi For Coding —— Bearer key,返回 5 小时(limits)和每周(usage)两档。

响应:{limits:[{detail:{limit,remaining,resetTime}}], usage:{limit,remaining,resetTime}}
  limit/remaining 是 token 数;已用 = limit - remaining;百分比 = 已用/limit*100。
"""
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_PERCENT, TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT
from api.provider_base import BearerKeyProvider, CredentialField
from api.registry import register
from api.time_utils import format_reset_time


@register
class KimiProvider(BearerKeyProvider):
    provider_id = "kimi"
    display_name = "Kimi"
    DEFAULT_URL = "https://api.kimi.com/coding/v1/usages"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        tiers: List[QuotaTier] = []

        # limits[] 是 5 小时滚动窗口;与 cc-switch 一致遍历全部条目
        # (实测通常只有 1 条,多条时每条各是一个 5h 档,不丢)
        for lim in body.get("limits", []):
            d = lim.get("detail") or {}
            limit = d.get("limit")
            remaining = d.get("remaining")
            if limit and limit > 0:
                used = limit - (remaining or 0)
                tiers.append(QuotaTier(
                    name=TIER_FIVE_HOUR, kind=KIND_PERCENT,
                    utilization=round(used / limit * 100, 1),
                    resets_at=format_reset_time(d.get("resetTime"))))

        # usage 是每周窗口
        u = body.get("usage") or {}
        limit = u.get("limit")
        remaining = u.get("remaining")
        if limit and limit > 0:
            used = limit - (remaining or 0)
            tiers.append(QuotaTier(
                name=TIER_WEEKLY_LIMIT, kind=KIND_PERCENT,
                utilization=round(used / limit * 100, 1),
                resets_at=format_reset_time(u.get("resetTime"))))

        return tiers, None
