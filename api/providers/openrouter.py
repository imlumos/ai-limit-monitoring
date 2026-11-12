"""OpenRouter —— Bearer key,信用额度(有总额度和已用,可合成百分比)。

响应:{data:{total_credits, total_usage}}
  total = total_credits,used = total_usage,remaining = total - used(单位 USD)。
  有 total+used → 能合成圆点配色用的利用率。
"""
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_MONEY, TIER_WALLET
from api.provider_base import BearerKeyProvider, CredentialField
from api.registry import register


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@register
class OpenRouterProvider(BearerKeyProvider):
    provider_id = "openrouter"
    display_name = "OpenRouter"
    DEFAULT_URL = "https://openrouter.ai/api/v1/credits"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        data = body.get("data") or {}
        total = _f(data.get("total_credits"))
        used = _f(data.get("total_usage"))
        remaining = (total - used) if (total is not None and used is not None) else None
        tier = QuotaTier(
            name=TIER_WALLET, kind=KIND_MONEY,
            total=total, used=used, remaining=remaining, unit="USD",
            is_valid=(remaining is None or remaining > 0))
        return [tier], None
