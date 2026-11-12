"""
GLM(智谱 AI)额度 Provider
============================

【这个文件做什么】
把原来 api/glm_client.py 里写死的 GLM 取额度逻辑,搬进统一的 Provider 框架。
行为完全一致:同样的接口、同样的请求头、同样的解析,只是换了个"符合统一接口"的壳,
这样它就能和将来加的 Kimi/DeepSeek 等平台并列、被同一套轮询/UI 代码调度。

【接口与鉴权(和原来一样,务必保持)】
  - GET https://open.bigmodel.cn/api/monitor/usage/quota/limit
  - Authorization 头直接放原始 API Key,【不带 "Bearer " 前缀】(所以继承 RawKeyProvider)
  - 额外头:Content-Type、Accept-Language(基类已加)

【响应结构】
  {code, success, msg, data: {level, limits: [{type, unit, percentage, nextResetTime}, ...]}}
  code==200 && success 才算成功;否则抛 ProviderError(业务失败)。

【档位识别:用 (type, unit) 二元组】
  TOKENS_LIMIT + unit 3  → 每 5 小时窗口  → TIER_FIVE_HOUR
  TOKENS_LIMIT + unit 6  → 每周窗口        → TIER_WEEKLY_LIMIT
  TIME_LIMIT   + unit 5  → MCP 每月窗口    → TIER_MONTHLY  ← 这条 cc-switch 故意丢弃,
                                                           但本项目保留(用户在用)
【unit 缺失时的兜底启发式】(从 cc-switch 移植,修上游 issue #3036)
  unit 不认识 ≠ 该条目没用:无 nextResetTime 的条目优先归 5h(5h 桶 0% 时可能没有
  reset),其余按重置时间升序填仍空缺的档位。为什么不能按时间排序分档——周期末尾
  每周窗口会比 5h 窗口更早重置,排序必然把两档标反。首次命中即占位,不覆盖。
"""
from enum import Enum
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT, TIER_MONTHLY, KIND_PERCENT
from api.provider_base import (RawKeyProvider, CredentialField, ProviderError,
                               CATEGORY_PERCENT)
from api.registry import register
from api.time_utils import format_reset_time_ms


# GLM 后端用 (type, unit) 二元组区分不同周期,提为 Enum 避免裸数字写错。
class _LimitType(Enum):
    TOKENS_LIMIT = "TOKENS_LIMIT"   # 按 token 数量限额
    TIME_LIMIT = "TIME_LIMIT"       # 按时间限额


class _LimitUnit(Enum):
    FIVE_HOURS = 3     # 5 小时
    MCP_MONTHLY = 5    # MCP 每月
    WEEKLY = 6         # 每周


@register
class GLMProvider(RawKeyProvider):
    """GLM(智谱)个人版 CN。"""

    provider_id = "glm"
    display_name = "GLM(智谱)"
    category = CATEGORY_PERCENT
    # 智谱额度接口默认地址(开放平台 open.bigmodel.cn)
    DEFAULT_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    credential_schema = [
        CredentialField("api_key", "API Key", secret=True,
                        placeholder="在 open.bigmodel.cn 控制台申请"),
    ]

    def _url(self, base_url: Optional[str], credentials: Dict[str, str]) -> str:
        # 允许 base_url 覆盖(一般用不到,留口子)
        return base_url or self.DEFAULT_URL

    def _parse(self, body, credentials: Dict[str, str]) -> Tuple[List[QuotaTier], Optional[str]]:
        """
        解析 GLM 响应体,返回(档位列表, 套餐 level)。

        body 形如:
            {code, success, msg, data:{level, limits:[{type,unit,percentage,nextResetTime}]}}
        """
        # 业务码双层判断(这种 API 的惯例),非成功抛 ProviderError(模板转失败快照)
        if body.get("code") != 200 or not body.get("success"):
            raise ProviderError(body.get("msg") or f"业务码非成功: code={body.get('code')}")

        data = body.get("data") or {}
        level = data.get("level")  # 套餐等级,作为 credential_message 显示

        tiers: List[QuotaTier] = []
        # 先按 (type, unit) 显式分档;首次命中即占位(同档多条不互相覆盖)
        matched = {}
        unclassified = []  # unit 不认识的条目,走兜底启发式
        for limit in data.get("limits", []):
            limit_type = limit.get("type")
            unit = limit.get("unit")
            percentage = limit.get("percentage", 0)
            reset_ms = limit.get("nextResetTime")

            if (limit_type == _LimitType.TOKENS_LIMIT.value
                    and unit == _LimitUnit.FIVE_HOURS.value):
                name = TIER_FIVE_HOUR
            elif (limit_type == _LimitType.TOKENS_LIMIT.value
                    and unit == _LimitUnit.WEEKLY.value):
                name = TIER_WEEKLY_LIMIT
            elif (limit_type == _LimitType.TIME_LIMIT.value
                    and unit == _LimitUnit.MCP_MONTHLY.value):
                name = TIER_MONTHLY
            else:
                name = None

            if name is not None:
                if name not in matched:          # 首次命中占位
                    matched[name] = (percentage, reset_ms)
            else:
                unclassified.append((reset_ms, percentage))

        # 兜底:无 reset 的条目优先归 5h(5h 桶 0% 时可能没有 reset),
        # 其余按 reset 升序填仍空缺的 weekly 槽。与 cc-switch 一致只兜底
        # 5h/weekly 两个槽——月档语义明确属于 TIME_LIMIT+unit5,不猜。
        unclassified.sort(key=lambda e: (e[0] is not None, e[0] or 0))
        for reset_ms, percentage in unclassified:
            if TIER_FIVE_HOUR not in matched and reset_ms is None:
                matched[TIER_FIVE_HOUR] = (percentage, reset_ms)
            elif (TIER_FIVE_HOUR not in matched and TIER_WEEKLY_LIMIT not in matched
                  and reset_ms is not None):
                # 没有无-reset 条目时,reset 最近的当 5h(上游约定:升序先填 5h)
                matched[TIER_FIVE_HOUR] = (percentage, reset_ms)
            elif TIER_WEEKLY_LIMIT not in matched and reset_ms is not None:
                matched[TIER_WEEKLY_LIMIT] = (percentage, reset_ms)

        # 按固定顺序产出档位(5h → 每周 → 每月),缺失就跳过
        for name in (TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT, TIER_MONTHLY):
            if name in matched:
                percentage, reset_ms = matched[name]
                tiers.append(QuotaTier(
                    name=name,
                    kind=KIND_PERCENT,
                    utilization=float(percentage) if percentage is not None else None,
                    resets_at=format_reset_time_ms(reset_ms),
                    # 月档语义特殊:TIME_LIMIT+unit5 统计的是【仅 MCP 工具调用】的
                    # 月度桶(调用次数×抵扣系数,官方只有 5h+每周两档积分,这个桶
                    # 官方文档没写)。档位名共用 TIER_MONTHLY(火山/Grok 是全量月度),
                    # 用 display_label/display_short_label 覆盖显示名,免得用户
                    # 误当成 coding plan 全量月额度。团队版/国际版继承本解析,一起生效。
                    display_label=("每月·MCP" if name == TIER_MONTHLY else None),
                    display_short_label=("MCP月" if name == TIER_MONTHLY else None),
                ))

        # 放宽:至少解析到一档(5h 或每周)才算成功;否则业务结构可能变了
        if not any(t.name in (TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT) for t in tiers):
            raise ProviderError("未解析到任何额度档位(接口结构可能已变化)")

        return tiers, level
