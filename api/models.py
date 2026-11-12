"""
统一额度模型 (models)
=====================

【这个文件做什么】
定义"所有平台"额度查询都会用到的统一数据结构:
  - QuotaTier:      一个额度档位(如:5 小时窗口 / 每周窗口 / 钱包余额)
  - QuotaSnapshot:  一次查询的完整结果(一个账号 + 若干档位 + 成功/失败状态)
  - 异常类:TransientFetchError(瞬时错误)、DeterministicError(确定性错误)
  - 档位名常量(TIER_FIVE_HOUR 等)和中文显示名

【为什么需要一个"统一"模型】
不同平台返回的额度数据长得完全不一样:
  - GLM / Kimi / MiniMax 返回"每个窗口已用百分比 0~100"
  - DeepSeek / OpenRouter 返回"钱包余额,还剩多少钱"
如果每个平台各定义一套数据结构,UI 就得对每种平台写一套显示代码——乱且重复。
所以把它们抽象成同一个模型,用 tier.kind 区分"百分比"还是"金额",UI 只写一份逻辑。
这正是参考的 cc-switch 项目里 UsageResult + SubscriptionQuota 两种结构的合体。

【关键概念:瞬时错误 vs 确定性错误】(cc-switch 的核心契约)
  - 瞬时错误(TransientFetchError):网络断了、请求超时、响应读到一半中断。
    这种错误"下次重试可能就好了",所以 UI 应保留上一次的好值,不要清空。
  - 确定性错误(DeterministicError):HTTP 401/403(Key 失效)、非 2xx、JSON 解析失败。
    这种错误"重试也没用",UI 应立刻显示错误信息。
区分这两类是为了网络抖动时悬浮窗不会忽明忽暗地闪烁、清空。

【关键概念:@dataclass】
@dataclass 自动生成 __init__、__repr__ 等样板代码,适合"主要用来装数据的类"。
字段都用 Optional + 默认 None:不同平台返回的档位数不一样,缺失某档不能让整体崩。
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)


# ====== 档位名常量(沿用 cc-switch 的命名,方便跨平台统一映射) ======
# 不同平台都把额度按"时间窗口"分档,这里给档位取统一的名字。
TIER_FIVE_HOUR = "five_hour"        # 每 5 小时滚动窗口
TIER_WEEKLY_LIMIT = "weekly_limit"  # 每周窗口(GLM/Kimi/MiniMax/ZenMux 等)
TIER_SEVEN_DAY = "seven_day"        # 每 7 天窗口(Claude/Codex 官方订阅)
TIER_MONTHLY = "monthly"            # 每月(含 GLM 的 MCP 月度)
TIER_THIRTY_DAY = "30_day"          # 每 30 天(Codex 免费档)
TIER_CREDITS = "credits"            # 信用/次数额度(Grok/Copilot)
TIER_WALLET = "wallet"              # 钱包余额(DeepSeek/OpenRouter 等金额类)

# 档位名 → 中文显示名(UI 用,改一处全局生效)
TIER_DISPLAY_NAMES = {
    TIER_FIVE_HOUR: "每5小时",
    TIER_WEEKLY_LIMIT: "每周",
    TIER_SEVEN_DAY: "每7天",
    "seven_day_opus": "每7天 Opus",
    "seven_day_sonnet": "每7天 Sonnet",
    TIER_MONTHLY: "每月",
    TIER_THIRTY_DAY: "每30天",
    TIER_CREDITS: "额度",
    TIER_WALLET: "余额",
    "gemini_pro": "Gemini Pro",
    "gemini_flash": "Gemini Flash",
    "gemini_flash_lite": "Gemini Flash Lite",
}

# 档位名 → 紧凑短标签(悬浮条这种窄地方用)
TIER_SHORT_LABELS = {
    TIER_FIVE_HOUR: "5h",
    TIER_WEEKLY_LIMIT: "7d",
    TIER_SEVEN_DAY: "7d",
    "seven_day_opus": "7d·Opus",
    "seven_day_sonnet": "7d·Son",
    TIER_MONTHLY: "月",
    TIER_THIRTY_DAY: "30d",
    TIER_CREDITS: "额度",
    TIER_WALLET: "余额",
    "gemini_pro": "Pro",
    "gemini_flash": "Flash",
    "gemini_flash_lite": "Lite",
}


def tier_display_name(tier) -> str:
    """取档位的中文显示名(详情面板用):优先 tier.display_label 覆盖,回落全局表。

    【为什么要有覆盖】TIER_MONTHLY 这类档位名是跨平台共用的(火山/Grok 也产月档),
    全局表只能给一个"每月"。但 GLM 的月档实际是"仅 MCP 工具用量"的月度桶(官方文档
    只写了 5h+每周两档积分,没提这个桶),provider 在 tier.display_label 里写
    "每月·MCP" 即可区分;其余平台不填 → 行为和以前完全一样。
    """
    label = getattr(tier, "display_label", None)
    if label:
        return label
    return TIER_DISPLAY_NAMES.get(tier.name, tier.name)


def tier_short_label(tier) -> str:
    """取档位的紧凑短标签(悬浮条用):优先 tier.display_short_label,回落全局表。

    短标签出现的地方极窄(侧边竖条仅 88px 宽),所以覆盖要单独给短版
    (GLM 月档 → "MCP月"),不能直接复用长名"每月·MCP"。
    """
    label = getattr(tier, "display_short_label", None)
    if label:
        return label
    return TIER_SHORT_LABELS.get(tier.name, tier.name)


def currency_symbol(unit: Optional[str]) -> str:
    """货币单位 → 符号(CNY→¥、USD→$),未知原样返回。UI 显示金额用。

    "REQUESTS"(Copilot 的次数单位)没有符号——它是计数不是货币,
    format_money 会按次数格式化(整数),不走货币分支。
    """
    u = (unit or "").upper()
    if u == "REQUESTS":
        return ""
    return {"CNY": "¥", "RMB": "¥", "USD": "$"}.get(u, unit or "")


def format_money(remaining: Optional[float], unit: Optional[str]) -> str:
    """把"剩余金额/次数"格式化成统一显示文本,如 ¥12.34 / -¥0.02 / 300 / --。

    【为什么要统一】浮窗小条和详情列表都显示金额,以前两边各写一套精度
    (浮窗 1 位小数、列表 2 位),导致同一账号在两处显示不一致(如 -0.0 vs -0.02)。
    集中在这里维护,改一处全局生效。
    【负数如实显示】用超了(used>total)时 remaining 为负,官网页面也是负数,
    所以这里如实展示(负号放货币符号前面,如 -¥0.02),不钳成 0。
    【次数类】unit="requests"(Copilot)是次数不是钱:不带货币符号、整数显示
    (300 而不是 requests300.00)。
    【不带"剩余"后缀】档位名"余额/钱包"已自解释,省掉后缀也让窄列(详情列表右侧)不被裁。
    """
    if not isinstance(remaining, (int, float)):
        return "--"
    val = float(remaining)
    if (unit or "").upper() == "REQUESTS":
        return f"{int(val)}"   # 次数:整数,无符号
    sym = currency_symbol(unit)
    if val < 0:
        return f"-{sym}{-val:.2f}"
    return f"{sym}{val:.2f}"


# ====== 两种"额度形态" ======
KIND_PERCENT = "percent"  # 百分比类:看 utilization(0~100 已用百分比)
KIND_MONEY = "money"      # 金额类:看 used / total / remaining(钱包余额)


class TransientFetchError(Exception):
    """
    瞬时错误:网络/超时/响应体中断。

    【谁来抛】api/_http.py 的 get_json 在网络异常时抛。
    【谁来接】轮询层(ui/poll_manager.py)捕获后,只更新 error_message、
             保留上一次的好值(keep-last-good),不清空档位。
    """
    pass


class DeterministicError(Exception):
    """
    确定性错误:HTTP 非 2xx(含 401/403)、JSON 解析失败。

    【status_code】HTTP 状态码(可能为 None,例如纯解析错误)。
    【raw_text】原始响应体片段(截断),用于日志排查。
    【谁来接】各 provider 把它转成 QuotaSnapshot(success=False),
             其中 401/403 → credential_status="expired"(Key 失效)。
    """
    def __init__(self, status_code: Optional[int], message: str, raw_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.raw_text = raw_text


@dataclass
class QuotaTier:
    """
    单个额度档位(纯数据容器)。

    【百分比类 tier(kind="percent")】只填 utilization(resets_at 可选),
       used/total/remaining 留 None。
    【金额类 tier(kind="money")】填 used/total/remaining/unit,
       utilization 可由 used/total 合成(给小圆点配色用),合成不了就 None。
    """
    name: str                              # 档位名,取上面 TIER_* 常量
    kind: str = KIND_PERCENT               # KIND_PERCENT | KIND_MONEY
    utilization: Optional[float] = None    # 0~100 已用百分比
    used: Optional[float] = None           # 金额:已用
    total: Optional[float] = None          # 金额:总额度上限
    remaining: Optional[float] = None      # 金额:剩余
    unit: Optional[str] = None             # "CNY" | "USD" | "AFP" ...
    resets_at: Optional[str] = None        # 已格式化的重置时间字符串
    is_valid: bool = True                  # 账号/余额是否有效(金额类可能 is_valid=False)
    display_label: Optional[str] = None    # 覆盖显示名(详情面板):同名档位不同平台语义
                                           # 不同时用,如 GLM 月档=仅 MCP 用量 → "每月·MCP"
    display_short_label: Optional[str] = None  # 覆盖短标签(悬浮条窄处):如 GLM 月档 → "MCP月"

    def effective_utilization(self) -> Optional[float]:
        """
        取"用于配色的已用百分比":优先用 utilization;金额类则按 used/total 合成。

        金额类合成不出(没有 total)时返回 None——UI 把它当灰色/黄色点。
        """
        if self.utilization is not None:
            return self.utilization
        # 金额类:按 used/total 合成一个百分比,只用于圆点配色
        if self.kind == KIND_MONEY and self.total not in (None, 0) and self.used is not None:
            try:
                return max(0.0, min(100.0, self.used / self.total * 100.0))
            except Exception:
                return None
        return None


@dataclass
class QuotaSnapshot:
    """
    一次额度查询的完整结果(对应一个账号)。

    【account_id / queried_at 为什么默认 None】
    provider 只关心"怎么取额度",不知道是哪个账号、几点取的;
    这两项由轮询层(AccountFetchWorker)在拿到快照后填上。
    所以 provider 构造时 account_id=None,worker 再赋值。

    【success=False 的两种情况】
    is_transient=True  → 瞬时错误,UI 保留上次值(不清空)
    is_transient=False → 确定性错误,UI 显示 error_message
    """
    account_id: Optional[str] = None
    provider_id: str = ""
    success: bool = True
    credential_status: str = "valid"        # valid | expired | not_found | parse_error
    credential_message: Optional[str] = None  # 套餐等级 / 备注信息(GLM 的 level 等)
    tiers: List[QuotaTier] = field(default_factory=list)
    error_message: Optional[str] = None
    is_transient: bool = False
    queried_at: Optional[int] = None        # epoch 毫秒,worker 填

    # ---- 两个工厂方法,让 provider 代码少写样板 ----
    @classmethod
    def success_snapshot(cls, provider_id: str, tiers: List[QuotaTier],
                         credential_message: Optional[str] = None) -> "QuotaSnapshot":
        """成功快照(account_id / queried_at 留给 worker 填)。"""
        return cls(provider_id=provider_id, success=True,
                   credential_status="valid", credential_message=credential_message,
                   tiers=tiers)

    @classmethod
    def failure_snapshot(cls, provider_id: str, *, credential_status: str,
                         error_message: str, is_transient: bool = False) -> "QuotaSnapshot":
        """失败快照。is_transient=True 表示瞬时错误(UI 保留上次值)。"""
        return cls(provider_id=provider_id, success=False,
                   credential_status=credential_status, error_message=error_message,
                   is_transient=is_transient)

    def worst_utilization(self) -> Optional[float]:
        """
        取所有"百分比类"档位里最高的已用百分比(用于小圆点配色 / 排序)。

        金额类档位的合成 utilization 也参与(更直观:快用完的钱包也该红)。
        一个档位都没有或都算不出时返回 None。
        """
        best = None
        for t in self.tiers:
            u = t.effective_utilization()
            if u is None:
                continue
            best = u if best is None else max(best, u)
        return best


def format_tier_value(tier: "QuotaTier") -> str:
    """把一个档位格式化成"右侧数值文字":金额→format_money;百分比→N%。

    浮窗小条和详情列表共用,保证两边同一档位显示完全一致。None 占位用 "--"。
    """
    if tier.kind == KIND_MONEY:
        return format_money(tier.remaining, tier.unit)
    u = tier.utilization
    return f"{int(u)}%" if u is not None else "--%"
