"""Cursor —— 用 Cursor 网页会话 cookie 查 cursor.com/api/usage-summary(参考 CodexBar)。

【凭据来源】
  1) 用户手填的 Cookie(可选):整段 "Cookie:" 请求头,或只填 WorkosCursorSessionToken 的值;
  2) 留空 → 读本机 Cursor 客户端的登录状态:
     <配置目录>/Cursor/User/globalStorage/state.vscdb 的 ItemTable['cursorAuth/accessToken'](JWT),
     拼成 WorkosCursorSessionToken=<userId>%3A%3A<accessToken>,userId = JWT sub 最后一个 "|" 之后的部分。
     Windows 下是明文 JWT;macOS 新版客户端可能加密存储,读不出时请手填 Cookie。
  token 由 Cursor 客户端自己续期,本程序只读不刷新;过期时提示打开 Cursor 重新登录。

【响应】金额字段单位是美分;*PercentUsed 已经是 0~100 的百分比。

【Grok Bot 周额度】(内部叫 "Sand")POST /api/dashboard/get-sand-usage-status,body {}。
  usagePercent 同样已是百分比;nextResetTimestampUtc 是每周重置时间(ISO)。
  这个接口是附加信息:失败/超时只丢掉这一档,不影响 Cursor 本身的用量。
  isUnified=true(统一套餐,Cursor 与 Grok 共用每周池)时,这一档取代月度套餐和 Auto/API 档。
"""
from datetime import datetime, timezone
import base64
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from api._http import get_json, post_json
from api.models import (QuotaSnapshot, QuotaTier, KIND_PERCENT, KIND_MONEY,
                        TIER_MONTHLY, DeterministicError, TransientFetchError)
from api.provider_base import QuotaProvider, CredentialField, ProviderError, CATEGORY_CUSTOM
from api.registry import register
from api.time_utils import format_reset_time_iso

logger = logging.getLogger(__name__)

USAGE_SUMMARY_URL = "https://cursor.com/api/usage-summary"
SAND_USAGE_URL = "https://cursor.com/api/dashboard/get-sand-usage-status"
SAND_TIMEOUT = 5.0
SESSION_COOKIE = "WorkosCursorSessionToken"
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_PLAN_NAMES = {"free": "Free", "pro": "Pro", "pro_plus": "Pro+", "ultra": "Ultra",
               "business": "Business", "enterprise": "Enterprise"}


def _state_db_candidates() -> List[Path]:
    """各系统 Cursor 客户端 state.vscdb 的位置(Windows / macOS / Linux)。"""
    home = Path.home()
    paths = []
    if os.environ.get("APPDATA"):
        paths.append(Path(os.environ["APPDATA"]))
    paths.append(home / "Library" / "Application Support")
    paths.append(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"))
    return [p / "Cursor" / "User" / "globalStorage" / "state.vscdb" for p in paths]


def _read_local_token() -> Optional[str]:
    for db in _state_db_candidates():
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=0.25)
            try:
                row = con.execute("SELECT value FROM ItemTable "
                                  "WHERE key = 'cursorAuth/accessToken' LIMIT 1").fetchone()
            finally:
                con.close()
        except sqlite3.Error as e:
            logger.warning("[cursor] 读取本地登录状态失败(%s)", type(e).__name__)
            continue
        if row and isinstance(row[0], str) and row[0].strip():
            return row[0].strip()
    return None


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _cookie_from_token(token: str) -> Tuple[Optional[str], Optional[str]]:
    """本地 JWT → (Cookie 头, 错误提示)。"""
    payload = _jwt_payload(token)
    user_id = str(payload.get("sub", "")).split("|")[-1]
    if not _USER_ID_RE.match(user_id):
        return None, "无法解析本机 Cursor 登录信息,请在账号里手动填写 Cookie"
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp <= time.time() + 60:
        return None, "本机 Cursor 登录已过期,请打开 Cursor 重新登录"
    return f"{SESSION_COOKIE}={user_id}%3A%3A{token}", None


def _cookie_from_manual(text: str) -> str:
    """手填内容:带 "=" 当整段 Cookie 头(可带 "Cookie:" 前缀),否则当会话 cookie 的值。"""
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    return text if "=" in text else f"{SESSION_COOKIE}={text}"


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _plan_percent(plan: dict) -> Optional[float]:
    """套餐已用%:totalPercentUsed → auto/api 平均 → 单项 → used/limit(与 CodexBar 同序)。"""
    total = _num(plan.get("totalPercentUsed"))
    if total is not None:
        return total
    auto, api = _num(plan.get("autoPercentUsed")), _num(plan.get("apiPercentUsed"))
    if auto is not None and api is not None:
        return (auto + api) / 2
    if api is not None or auto is not None:
        return api if api is not None else auto
    used, limit = _num(plan.get("used")), _num(plan.get("limit"))
    if used is not None and limit:
        return used / limit * 100
    return None


def _clamp(p: float) -> float:
    return max(0.0, min(100.0, p))


def parse_usage_summary(body) -> Tuple[List[QuotaTier], Optional[str]]:
    if not isinstance(body, dict):
        raise ProviderError("Cursor 响应格式无效")
    resets = format_reset_time_iso(body.get("billingCycleEnd")) or None
    individual = body.get("individualUsage") or {}
    plan = individual.get("plan") or {}
    # 团队/企业成员没有个人 plan 时,退回个人上限(overall)或团队池(pooled)
    if not plan:
        plan = individual.get("overall") or (body.get("teamUsage") or {}).get("pooled") or {}

    tiers: List[QuotaTier] = []
    percent = _plan_percent(plan)
    if percent is not None:
        tiers.append(QuotaTier(name=TIER_MONTHLY, kind=KIND_PERCENT,
                               utilization=_clamp(percent), resets_at=resets))
    for key, label in (("autoPercentUsed", "Auto"), ("apiPercentUsed", "API")):
        p = _num(plan.get(key))
        if p is not None:
            tiers.append(QuotaTier(name=f"cursor_{label.lower()}", kind=KIND_PERCENT,
                                   utilization=_clamp(p), resets_at=resets,
                                   display_label=f"每月·{label}", display_short_label=label))

    on_demand = individual.get("onDemand") or {}
    if on_demand.get("enabled"):
        used = (_num(on_demand.get("used")) or 0) / 100
        limit = _num(on_demand.get("limit"))
        total = limit / 100 if limit is not None else None
        tiers.append(QuotaTier(name="cursor_on_demand", kind=KIND_MONEY, used=used, total=total,
                               remaining=(total - used) if total is not None else None,
                               unit="USD", resets_at=resets,
                               display_label="按量付费", display_short_label="按量"))

    plan_type = body.get("membershipType")
    message = f"Cursor {_PLAN_NAMES.get(plan_type, plan_type)}" if plan_type else None
    return tiers, message


def _parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_sand_usage(body, now: Optional[datetime] = None) -> Tuple[Optional[QuotaTier], bool]:
    """Grok Bot 用量 → (档位或 None, 是否统一套餐共用每周池)。

    有非零包含额度,或在有效试用期内,才算有这一档(与 CodexBar 一致);试用期没有每周重置。
    """
    if not isinstance(body, dict):
        return None, False
    now = now or datetime.now(timezone.utc)
    zero = body.get("includedLimitZero")
    has_limit = (not zero) if isinstance(zero, bool) else body.get("hasNonZeroIncludedLimit") is True
    trial_end = _parse_iso(body.get("sandTrialExpiresAt"))
    has_trial = not has_limit and trial_end is not None and trial_end > now
    percent = _num(body.get("usagePercent"))
    if not (has_limit or has_trial) or percent is None:
        return None, False
    reset = None if has_trial else _parse_iso(body.get("nextResetTimestampUtc"))
    tier = QuotaTier(name="cursor_grok_bot", kind=KIND_PERCENT, utilization=_clamp(percent),
                     resets_at=(format_reset_time_iso(reset.isoformat()) or None) if reset else None,
                     display_label="Grok Bot·试用" if has_trial else "Grok Bot·每周",
                     display_short_label="Grok")
    shared_pool = body.get("isUnified") is True and reset is not None and reset > now
    return tier, shared_pool


@register
class CursorProvider(QuotaProvider):
    provider_id = "cursor"
    display_name = "Cursor"
    category = CATEGORY_CUSTOM
    credential_schema = [
        CredentialField("cookie", "Cookie(可选)", required=False,
                        placeholder="留空 = 读取本机 Cursor 客户端的登录状态"),
    ]

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        manual = (credentials.get("cookie") or "").strip()
        if manual:
            cookie = _cookie_from_manual(manual)
        else:
            token = _read_local_token()
            if not token:
                return QuotaSnapshot.failure_snapshot(
                    self.provider_id, credential_status="not_found",
                    error_message="未找到本机 Cursor 登录状态(请先登录 Cursor,或手动填写 Cookie)")
            cookie, msg = _cookie_from_token(token)
            if not cookie:
                return QuotaSnapshot.failure_snapshot(
                    self.provider_id, credential_status="expired", error_message=msg)

        headers = {"Accept": "application/json", "Cookie": cookie}
        try:
            body, _status = get_json(self._session, USAGE_SUMMARY_URL, headers)
        except DeterministicError as e:
            return self._failure_from_deterministic(e)
        # TransientFetchError 原样上抛,轮询层保留上次好值

        try:
            tiers, message = parse_usage_summary(body)
        except ProviderError as e:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message=str(e))
        sand, shared_pool = self._fetch_sand(headers)
        if sand is not None:
            if shared_pool:
                # 统一套餐:Cursor 与 Grok 共用每周池,月度套餐/Auto/API 不再代表真实额度
                tiers = [sand] + [t for t in tiers if t.kind == KIND_MONEY]
            else:
                money_at = next((i for i, t in enumerate(tiers) if t.kind == KIND_MONEY), len(tiers))
                tiers.insert(money_at, sand)
        if not tiers:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error",
                error_message="未解析到 Cursor 用量,可能是不支持的套餐类型")
        logger.info("[cursor] 取额度成功:%d 档", len(tiers))
        return QuotaSnapshot.success_snapshot(self.provider_id, tiers, credential_message=message)

    def _fetch_sand(self, headers: Dict[str, str]) -> Tuple[Optional[QuotaTier], bool]:
        """Grok Bot 周额度;任何失败都只记日志、返回 (None, False)。"""
        try:
            body, _ = post_json(self._session, SAND_USAGE_URL, headers, {}, timeout=SAND_TIMEOUT,
                                extra_headers={"Content-Type": "application/json",
                                               "Origin": "https://cursor.com"})
            return parse_sand_usage(body)
        except (TransientFetchError, DeterministicError) as e:
            logger.info("[cursor] Grok Bot 用量获取失败,跳过(%s)", type(e).__name__)
        except Exception as e:
            logger.warning("[cursor] Grok Bot 用量解析失败,跳过(%s)", type(e).__name__)
        return None, False
