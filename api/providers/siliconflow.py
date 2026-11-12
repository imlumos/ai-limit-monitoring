"""硅基流动 SiliconFlow —— Bearer key,钱包余额(默认国内站)。

响应:{code, data:{balance, chargeBalance, totalBalance, status}}
  remaining = data.totalBalance(国内 CNY;国际站 base_url 填 api.siliconflow.com 则 USD)。
"""
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_MONEY, TIER_WALLET
from api.provider_base import BearerKeyProvider, CredentialField, ProviderError
from api.registry import register


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@register
class SiliconFlowProvider(BearerKeyProvider):
    provider_id = "siliconflow"
    display_name = "硅基流动"
    DEFAULT_URL = "https://api.siliconflow.cn/v1/user/info"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        # 记下实际请求地址,_parse 据此判断国内站(CNY)/国际站(USD)
        self._effective_url = base_url or self.DEFAULT_URL
        return self._effective_url

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        if body.get("code") not in (None, 200, "200", 0):
            raise ProviderError(f"业务码异常: code={body.get('code')}")
        data = body.get("data") or {}
        total = _f(data.get("totalBalance"))
        url = getattr(self, "_effective_url", self.DEFAULT_URL).lower()
        # 域名精确判断国际站(USD):host 就是 api.siliconflow.com;
        # 国内站 api.siliconflow.cn 及其他一律 CNY。子串匹配会误判
        # (如 "not-siliconflow.com.evil.cn" 这种 base_url)
        host = url.split("//")[-1].split("/")[0]
        unit = "USD" if host == "api.siliconflow.com" else "CNY"
        tiers = [QuotaTier(name=TIER_WALLET, kind=KIND_MONEY, remaining=total, unit=unit)]
        return tiers, None
