"""StepFun(阶跃星辰)—— Bearer key,钱包余额。

响应:{object, type, balance, total_cash_balance, total_voucher_balance}
  remaining = balance(可用余额,单位 CNY)。
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
class StepFunProvider(BearerKeyProvider):
    provider_id = "stepfun"
    display_name = "StepFun"
    DEFAULT_URL = "https://api.stepfun.com/v1/accounts"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        bal = _f(body.get("balance"))
        tiers = [QuotaTier(name=TIER_WALLET, kind=KIND_MONEY, remaining=bal, unit="CNY")]
        return tiers, None
