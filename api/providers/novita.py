"""Novita AI —— Bearer key,钱包余额。

响应:{availableBalance, cashBalance, creditLimit, outstandingInvoices}
  availableBalance 单位是 0.0001 USD → 要除以 10000。
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
class NovitaProvider(BearerKeyProvider):
    provider_id = "novita"
    display_name = "Novita AI"
    DEFAULT_URL = "https://api.novita.ai/v3/user/balance"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        raw = body.get("availableBalance")
        avail = _f(raw)
        remaining = (avail / 10000.0) if avail is not None else None  # 单位换算
        tiers = [QuotaTier(name=TIER_WALLET, kind=KIND_MONEY, remaining=remaining, unit="USD")]
        return tiers, None
