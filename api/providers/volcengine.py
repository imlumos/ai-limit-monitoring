"""火山方舟(Volcengine)—— AK/SK 签名(Volcengine SigV4 变体)+ 控制面 OpenAPI。

【为什么特殊】签名是字节精确的,且和标准 AWS SigV4 有 4 处偏差,不能用通用库:
  1) canonical headers 固定顺序 host;x-date;x-content-sha256;content-type(非字母序)
  2) 算法串是 "HMAC-SHA256"(无 AWS4- 前缀)
  3) credential scope 后缀是 "request"(非 aws4_request),service="ark"
  4) 签名密钥链用【原始 SK】(不加 AWS4 前缀)

【流程】先 GetAFPUsage(Agent Plan);解析为空或软错误 → 回退 GetCodingPlanUsage。
  401/403 或 auth 类错误码 → Key 失效;网络错误 → 瞬时。
"""
import hashlib
import hmac
import json
import logging
import datetime
from typing import List, Optional, Tuple, Dict

from api.models import (QuotaSnapshot, QuotaTier, KIND_PERCENT,
                        TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT, TIER_MONTHLY)
from api.provider_base import QuotaProvider, CredentialField, ProviderError, CATEGORY_CUSTOM
from api.registry import register
from api._http import post_json, TransientFetchError, DeterministicError
from api.time_utils import format_reset_time_seconds, format_reset_time_ms

logger = logging.getLogger(__name__)

_OPENAPI_HOST = "open.volcengineapi.com"
_API_VERSION = "2024-01-01"
_DEFAULT_REGION = "cn-beijing"
_SERVICE = "ark"
_CONTENT_TYPE = "application/json; charset=utf-8"
_SIGNED_HEADERS = "host;x-date;x-content-sha256;content-type"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ====== 签名核心(逐字对照 cc-switch coding_plan.rs) ======
def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uri_encode(s: str) -> str:
    """RFC3986 非保留字符(A-Za-z0-9-_.~)原样,其余按字节 %XX(大写)。"""
    out = []
    for byte in s.encode("utf-8"):
        if (65 <= byte <= 90 or 97 <= byte <= 122 or 48 <= byte <= 57
                or byte in (45, 95, 46, 126)):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def _canonical_query(action: str, region: str) -> str:
    pairs = sorted([("Action", action), ("Region", region), ("Version", _API_VERSION)])
    return "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in pairs)


def _sign(ak: str, sk: str, region: str, query: str, body: bytes, now=None):
    """返回 (authorization, x_date, x_content_sha256)。now 可注入(测试用)。"""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    x_content_sha256 = _sha256_hex(body)
    canonical_headers = (
        f"host:{_OPENAPI_HOST}\n"
        f"x-date:{x_date}\n"
        f"x-content-sha256:{x_content_sha256}\n"
        f"content-type:{_CONTENT_TYPE}\n"
    )
    canonical_request = (f"POST\n/\n{query}\n{canonical_headers}\n"
                         f"{_SIGNED_HEADERS}\n{x_content_sha256}")
    scope = f"{short_date}/{region}/{_SERVICE}/request"
    string_to_sign = (f"HMAC-SHA256\n{x_date}\n{scope}\n"
                      f"{_sha256_hex(canonical_request.encode('utf-8'))}")
    # 签名密钥链:原始 SK(不加 AWS4 前缀),终止串 "request"
    k_date = _hmac_sha256(sk.encode("utf-8"), short_date.encode("utf-8"))
    k_region = _hmac_sha256(k_date, region.encode("utf-8"))
    k_service = _hmac_sha256(k_region, _SERVICE.encode("utf-8"))
    k_signing = _hmac_sha256(k_service, b"request")
    signature = _hmac_sha256(k_signing, string_to_sign.encode("utf-8")).hex()
    auth = (f"HMAC-SHA256 Credential={ak}/{scope}, "
            f"SignedHeaders={_SIGNED_HEADERS}, Signature={signature}")
    return auth, x_date, x_content_sha256


def _is_auth_error_code(code: Optional[str]) -> bool:
    if not code:
        return False
    c = code.lower()
    return any(k in c for k in ("auth", "signature", "accessdenied", "denied",
                                "unauthorized", "forbidden", "credential", "token"))


def _extract_error(raw_text: str):
    """从响应体解析 (Code, Message);解析失败返回 (None, None)。"""
    try:
        body = json.loads(raw_text)
    except (ValueError, TypeError):
        return None, None
    err = (body.get("ResponseMetadata") or {}).get("Error") or body.get("Error")
    if isinstance(err, dict):
        return err.get("Code"), err.get("Message")
    return None, None


@register
class VolcengineProvider(QuotaProvider):
    provider_id = "volcengine"
    display_name = "火山方舟"
    category = CATEGORY_CUSTOM
    # AK/SK 是控制面凭据(和推理 API Key 不是一回事),都敏感
    credential_schema = [
        CredentialField("access_key_id", "Access Key ID", secret=True,
                        placeholder="AKLT..."),
        CredentialField("secret_access_key", "Secret Access Key", secret=True),
    ]

    # ---- 单次 OpenAPI 调用,分类返回 ----
    def _safe_call(self, action: str, region: str, ak: str, sk: str):
        """返回 ('ok', body) | ('auth', msg) | ('soft', msg) | ('transient', None)"""
        query = _canonical_query(action, region)
        auth, x_date, x_content_sha256 = _sign(ak, sk, region, query, b"")
        url = f"https://{_OPENAPI_HOST}/?{query}"
        headers = {"X-Date": x_date, "X-Content-Sha256": x_content_sha256,
                   "Content-Type": _CONTENT_TYPE, "Authorization": auth}
        try:
            body, _status = post_json(self._session, url, headers, body=None)
        except TransientFetchError:
            return ("transient", None)
        except DeterministicError as e:
            code, msg = _extract_error(e.raw_text)
            if e.status_code in (401, 403) or _is_auth_error_code(code):
                return ("auth", f"{code}: {msg}" if code else f"HTTP {e.status_code}")
            return ("soft", f"{code}: {msg}" if code else f"HTTP {e.status_code}")
        # 2xx 也可能内嵌业务错误
        code, msg = _extract_error(json.dumps(body))
        if code and _is_auth_error_code(code):
            return ("auth", f"{code}: {msg}")
        if code:
            return ("soft", f"{code}: {msg}")
        return ("ok", body)

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        ak = credentials.get("access_key_id", "")
        sk = credentials.get("secret_access_key", "")
        region = _DEFAULT_REGION

        soft_errors = []
        # 1) Agent Plan(GetAFPUsage)
        kind, info = self._safe_call("GetAFPUsage", region, ak, sk)
        if kind == "auth":
            return QuotaSnapshot.failure_snapshot(self.provider_id,
                                                  credential_status="expired", error_message=info)
        if kind == "transient":
            raise TransientFetchError("火山方舟网络错误")
        if kind == "ok":
            result = (info or {}).get("Result") or info or {}
            tiers = self._parse_afp(result)
            if tiers:
                plan = str(result.get("PlanType", "")).strip()
                return QuotaSnapshot.success_snapshot(
                    self.provider_id, tiers,
                    credential_message=f"Agent Plan {plan}" if plan else "Agent Plan")
        else:
            soft_errors.append(info)

        # 2) Coding Plan(GetCodingPlanUsage)
        kind2, info2 = self._safe_call("GetCodingPlanUsage", region, ak, sk)
        if kind2 == "auth":
            return QuotaSnapshot.failure_snapshot(self.provider_id,
                                                  credential_status="expired", error_message=info2)
        if kind2 == "transient":
            raise TransientFetchError("火山方舟网络错误")
        if kind2 == "ok":
            result = (info2 or {}).get("Result") or info2 or {}
            tiers = self._parse_coding(result)
            if tiers:
                return QuotaSnapshot.success_snapshot(self.provider_id, tiers,
                                                      credential_message="Coding Plan")
        elif kind2 == "soft":
            soft_errors.append(info2)

        msg = "; ".join(soft_errors) if soft_errors else "未找到有效的 Agent Plan / Coding Plan 订阅"
        logger.warning("[volcengine] 无可用额度")
        return QuotaSnapshot.failure_snapshot(self.provider_id,
                                              credential_status="parse_error", error_message=msg)

    # ---- 解析 ----
    @staticmethod
    def _volc_reset(v) -> str:
        n = _int(v)
        if n is None or n <= 0:
            return ""
        if n < 1_000_000_000_000:   # 秒
            return format_reset_time_seconds(n)
        return format_reset_time_ms(n)   # 毫秒

    def _parse_afp(self, result: dict) -> List[QuotaTier]:
        tiers = []
        for key, name in (("AFPFiveHour", TIER_FIVE_HOUR),
                          ("AFPWeekly", TIER_WEEKLY_LIMIT),
                          ("AFPMonthly", TIER_MONTHLY)):
            win = result.get(key)
            if not isinstance(win, dict):
                continue
            quota = _f(win.get("Quota")) or 0.0
            if quota <= 0:
                continue  # 未订阅该档 → 跳过(全空会触发 Coding Plan 回退)
            used = _f(win.get("Used")) or 0.0
            tiers.append(QuotaTier(
                name=name, kind=KIND_PERCENT,
                utilization=used / quota * 100.0,
                resets_at=self._volc_reset(win.get("ResetTime"))))
        return tiers

    def _parse_coding(self, result: dict) -> List[QuotaTier]:
        arr = (result.get("QuotaUsage") or result.get("Usages")
               or result.get("Details") or [])
        tiers = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            label = None
            for k in ("Level", "Type", "Period", "Label", "Window"):
                v = item.get(k)
                if isinstance(v, str):
                    label = v
                    break
            name = _coding_window(label.lower() if label else "")
            if not name:
                continue
            pct = (_f(item.get("Percent")) or _f(item.get("UsedPercent"))
                   or _f(item.get("UsagePercent")) or 0.0)
            tiers.append(QuotaTier(
                name=name, kind=KIND_PERCENT, utilization=pct,
                resets_at=self._volc_reset(item.get("ResetTimestamp")
                                           or item.get("ResetTime"))))
        return tiers


def _coding_window(label: str) -> Optional[str]:
    """Coding Plan 的窗口标签 → 档位名。

    session 系标签映射到 five_hour(显示"5h"):2026-08-20 实测火山 Coding Plan
    的 session 档确实是 5 小时滚动窗口(此前以为"会话额度无固定周期",已证伪),
    与 cc-switch 上游保持一致。
    """
    if label in ("session", "5h", "fivehour", "five_hour", "rolling_5h"):
        return TIER_FIVE_HOUR
    if label in ("weekly", "week", "7d"):
        return TIER_WEEKLY_LIMIT
    if label in ("monthly", "month"):
        return TIER_MONTHLY
    logger.debug("[volcengine] Coding Plan 未知窗口标签,跳过")
    return None
