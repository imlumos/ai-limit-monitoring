"""DeepSeek —— Bearer key,钱包余额(可能多币种,各一条)。

响应:{balance_infos:[{currency, total_balance, granted_balance, topped_up_balance}],
       is_available}
  remaining = total_balance,unit = currency(一般是 CNY)。
  多币种时两条档位都叫"余额"分不清——币种列表并进 credential_message
  (如 "CNY + USD"),档名保持 TIER_WALLET 不动(档名是 UI 映射键,不能乱改)。
  is_available=False 时 is_valid=False,UI 会画警示标记。
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
class DeepSeekProvider(BearerKeyProvider):
    provider_id = "deepseek"
    display_name = "DeepSeek"
    DEFAULT_URL = "https://api.deepseek.com/user/balance"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        is_avail = body.get("is_available", True)
        tiers: List[QuotaTier] = []
        currencies = []
        for info in body.get("balance_infos", []):
            total = _f(info.get("total_balance"))
            currency = info.get("currency") or "CNY"
            if currency not in currencies:
                currencies.append(currency)
            tiers.append(QuotaTier(
                name=TIER_WALLET, kind=KIND_MONEY,
                remaining=total, unit=currency, is_valid=bool(is_avail)))
        # 多币种时档名都叫"余额",把币种列表放备注行帮用户区分两行
        message = " + ".join(currencies) if len(currencies) > 1 else None
        return tiers, message
