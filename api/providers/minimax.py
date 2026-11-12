"""MiniMax —— Bearer key,返回 5 小时和每周两档(注意是"剩余"百分比,要反转为已用)。

响应:{base_resp:{status_code,status_msg},
       model_remains:[{model_name:"general"|"video",
                       current_interval_remaining_percent,  # 5h 剩余%
                       current_weekly_remaining_percent,    # 每周剩余%
                       current_weekly_status,               # 1=有每周额度,3=无
                       end_time, weekly_end_time}]}
只取 model_name=="general"(忽略 video)。已用% = 100 - 剩余%。
"""
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_PERCENT, TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT
from api.provider_base import BearerKeyProvider, CredentialField, ProviderError
from api.registry import register
from api.time_utils import format_reset_time_ms


@register
class MiniMaxProvider(BearerKeyProvider):
    provider_id = "minimax"
    display_name = "MiniMax"
    DEFAULT_URL = "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains"
    credential_schema = [CredentialField("api_key", "API Key", secret=True)]

    def _url(self, base_url, credentials):
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials) -> Tuple[List[QuotaTier], Optional[str]]:
        # 业务码校验:status_code 非 0/200 视为失败
        base = body.get("base_resp") or {}
        status = base.get("status_code")
        if status not in (None, 0, 200):
            raise ProviderError(base.get("status_msg") or f"status_code={status}")

        tiers: List[QuotaTier] = []
        for m in body.get("model_remains", []):
            if m.get("model_name") != "general":
                continue  # 跳过 video 等
            interval_rem = m.get("current_interval_remaining_percent")
            if interval_rem is not None:
                tiers.append(QuotaTier(
                    name=TIER_FIVE_HOUR, kind=KIND_PERCENT,
                    utilization=round(100 - interval_rem, 1),  # 剩余 → 已用
                    resets_at=format_reset_time_ms(m.get("end_time"))))
            # weekly_status==1 才有每周额度(3 表示无,跳过)
            if m.get("current_weekly_status") == 1:
                weekly_rem = m.get("current_weekly_remaining_percent")
                if weekly_rem is not None:
                    tiers.append(QuotaTier(
                        name=TIER_WEEKLY_LIMIT, kind=KIND_PERCENT,
                        utilization=round(100 - weekly_rem, 1),
                        resets_at=format_reset_time_ms(m.get("weekly_end_time"))))
            break  # 只取 general 一组
        return tiers, None
