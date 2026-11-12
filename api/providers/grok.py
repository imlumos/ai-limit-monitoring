"""Grok(xAI SuperGrok)—— 读 ~/.grok/auth.json 的 token,POST gRPC-web,解析二进制 protobuf。

【为什么特殊】响应是二进制 gRPC-web+proto(无 .proto),只能启发式扫描:
  1) gRPC-web 分帧:每帧 5 字节头(1 flags + 4 字节大端长度),flags&0x80 是 trailer 跳过。
  2) 扫 protobuf:wire-type 5(32 位小端 float)路径末位==1 且值∈[0,100] → 用量%;
     wire-type 0(varint)值∈[1.7e9,2.1e9] 且未来时刻 → 重置时间戳。
  3) 用量% 取"最浅路径 + 最早序号";重置时间优先精确路径 [1,5,1]。
  4) proto3 省略 0 值:没有任何 float 字段、有 usage-period 标记、有重置时间 → 用量算 0%。

【脆性】xAI 改字段顺序就会失效;解析失败按确定性错误降级(不会崩)。
"""
import logging
import math
import struct
import time
from typing import List, Optional, Tuple, Dict

from api.models import (QuotaSnapshot, QuotaTier, KIND_PERCENT,
                        TIER_WEEKLY_LIMIT, TIER_MONTHLY, TIER_CREDITS)
from api.provider_base import QuotaProvider, CredentialField, ProviderError, CATEGORY_CUSTOM
from api.registry import register
from api.time_utils import format_reset_time_seconds
from api._http import TransientFetchError

import requests

logger = logging.getLogger(__name__)

GROK_BILLING_ENDPOINT = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
OIDC_SCOPE_PREFIX = "https://auth.x.ai::"
LEGACY_SESSION_SCOPE = "https://accounts.x.ai/sign-in"


# ====== gRPC-web 分帧 ======
def _grpc_web_data_frames(data: bytes) -> List[bytes]:
    """拆出数据帧(flags&0x80 的 trailer 跳过)。任何帧越界 → 整体返回 [](表示非分帧)。"""
    frames = []
    i = 0
    n = len(data)
    while i < n:
        if i + 5 > n:
            return []
        flags = data[i]
        length = int.from_bytes(data[i + 1:i + 5], "big")
        start = i + 5
        end = start + length
        if end > n:
            return []
        if flags & 0x80 == 0:
            frames.append(data[start:end])
        i = end
    return frames


def _looks_like_protobuf(data: bytes) -> bool:
    if not data:
        return False
    first = data[0]
    return (first >> 3) > 0 and ((first & 0x07) in (0, 1, 2, 5))


# ====== gRPC-web trailer 错误解析(逐字对照 cc-switch subscription_grok.rs) ======
def _grpc_web_trailer_fields(data: bytes) -> Dict[str, str]:
    """从 trailer 帧(flags&0x80)解析 grpc-status / grpc-message 等字段。

    gRPC 的错误不走 HTTP 状态码:token 失效时 HTTP 仍是 200,错误藏在
    trailer 帧的 "grpc-status: 16" / "grpc-message: ..." 里(trailers-only
    响应甚至连数据帧都没有,整个响应就一个 trailer)。
    """
    fields: Dict[str, str] = {}
    i = 0
    n = len(data)
    while i + 5 <= n:
        flags = data[i]
        length = int.from_bytes(data[i + 1:i + 5], "big")
        start = i + 5
        end = start + length
        if end > n:
            break
        if flags & 0x80:  # trailer 帧:文本格式 "key: value" 每行一条
            try:
                text = data[start:end].decode("utf-8")
                for line in text.splitlines():
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    fields[key.strip().lower()] = _percent_decode(value.strip())
            except UnicodeDecodeError:
                pass
        i = end
    return fields


def _percent_decode(s: str) -> str:
    """gRPC message 用 percent-encoding;解码失败的序列原样保留。"""
    out = []
    i = 0
    b = s.encode("utf-8", "replace")
    while i < len(b):
        if b[i:i + 1] == b"%" and i + 2 < len(b):
            try:
                out.append(bytes([int(b[i + 1:i + 3].decode("ascii"), 16)]))
                i += 3
                continue
            except (ValueError, UnicodeDecodeError):
                pass
        out.append(b[i:i + 1])
        i += 1
    return b"".join(out).decode("utf-8", "replace")


# ====== protobuf 扫描 ======
def _read_varint(b: bytes, i: int) -> Tuple[Optional[int], int]:
    val = 0
    shift = 0
    while i < len(b) and shift < 64:
        byte = b[i]
        i += 1
        val |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return val, i
        shift += 7
    return None, i


class _Scan:
    __slots__ = ("fixed32_fields", "varint_fields")

    def __init__(self):
        self.fixed32_fields = []  # [(path, float, order)]
        self.varint_fields = []   # [(path, int)]


def _scan_protobuf(b: bytes, depth: int, path: List[int], order: int, scan: _Scan) -> int:
    i = 0
    n = len(b)
    next_order = order
    while i < n:
        field_start = i
        key, i = _read_varint(b, i)
        if key is None or key == 0:
            i = field_start + 1
            continue
        field_number = key >> 3
        wire_type = key & 0x07
        fpath = path + [field_number]
        if wire_type == 0:  # varint
            val, i = _read_varint(b, i)
            if val is None:
                i = field_start + 1
            else:
                scan.varint_fields.append((fpath, val))
        elif wire_type == 1:  # 64-bit
            if i + 8 > n:
                return next_order
            i += 8
        elif wire_type == 2:  # length-delimited
            length, i = _read_varint(b, i)
            if length is None or length > n - i:
                i = field_start + 1
                continue
            end = i + length
            if depth < 4:
                next_order = _scan_protobuf(b[i:end], depth + 1, fpath, next_order, scan)
            i = end
        elif wire_type == 5:  # 32-bit float
            if i + 4 > n:
                return next_order
            fval = struct.unpack("<f", b[i:i + 4])[0]
            scan.fixed32_fields.append((fpath, fval, next_order))
            next_order += 1
            i += 4
        else:
            i = field_start + 1
    return next_order


def _parse_billing_payload(data: bytes, now_secs: int) -> Tuple[float, Optional[int]]:
    """返回 (used_percent, resets_at_seconds)。解析不出抛 ProviderError。"""
    frames = _grpc_web_data_frames(data)
    if not frames and _looks_like_protobuf(data):
        frames = [data]
    if not frames:
        raise ProviderError("Grok 响应中没有 protobuf payload")

    scan = _Scan()
    for payload in frames:
        _scan_protobuf(payload, 0, [], 0, scan)

    # 用量%:fixed32,路径末位==1,值 finite 且 ∈[0,100];最浅路径 + 最早序号
    cand = [(p, v, o) for (p, v, o) in scan.fixed32_fields
            if p and p[-1] == 1 and math.isfinite(v) and 0.0 <= v <= 100.0]
    parsed = None
    if cand:
        cand.sort(key=lambda t: (len(t[0]), t[2]))
        parsed = float(cand[0][1])

    # 重置时间:varint ∈[1.7e9,2.1e9] 且未来;优先精确路径 [1,5,1]
    reset_cands = [(p, v) for (p, v) in scan.varint_fields
                   if 1_700_000_000 <= v <= 2_100_000_000 and v > now_secs]
    exact = [v for (p, v) in reset_cands if p == [1, 5, 1]]
    reset = min(exact) if exact else (min(v for _, v in reset_cands) if reset_cands else None)

    # proto3 零用量特判:没有任何 float 字段 + 有 usage-period 标记 + 有重置时间
    has_usage = any(p[:2] == [1, 6] or (p == [1, 8, 1] and v in (1, 2))
                    for p, v in scan.varint_fields)
    no_usage_yet = (parsed is None and not scan.fixed32_fields
                    and reset is not None and has_usage)
    used = parsed if parsed is not None else (0.0 if no_usage_yet else None)
    if used is None:
        raise ProviderError("无法定位 Grok 用量百分比")
    return used, reset


def _tier_name_for_reset(resets_at: Optional[int], now_secs: int) -> str:
    if resets_at is not None:
        days = round((resets_at - now_secs) / 86400)
        if 4 <= days <= 12:
            return TIER_WEEKLY_LIMIT
        if 20 <= days <= 45:
            return TIER_MONTHLY
    return TIER_CREDITS


# ====== gRPC 错误分类(对照 cc-switch is_*_grpc_status 三个判定) ======
def _is_grpc_auth_failure(status: int, message: str) -> bool:
    """16=UNAUTHENTICATED;7=PERMISSION_DENIED 但文案是凭据类才算。"""
    if status == 16:
        return True
    if status != 7:
        return False
    low = message.lower()
    return ("bad-credentials" in low or "unauthenticated" in low
            or ("oauth2" in low and "could not be validated" in low)
            or ("access token" in low
                and any(k in low for k in ("invalid", "expired", "could not be validated"))))


def _is_team_billing_unavailable(status: int, message: str) -> bool:
    """status 9 + "no personal team":xAI 没给团队主体开用量接口,提示可读化。"""
    return status == 9 and message.strip().lower() in ("no personal team", "no personal team.")


def _is_transient_grpc_status(status: int, message: str) -> bool:
    """4=DEADLINE_EXCEEDED / 14=UNAVAILABLE(等价 HTTP 504/503);
    1=CANCELLED 仅带超时文案才算瞬时。"""
    if status in (4, 14):
        return True
    if status == 1:
        low = message.lower()
        return any(k in low for k in ("timeout", "deadline", "expired"))
    return False


@register
class GrokProvider(QuotaProvider):
    provider_id = "grok"
    display_name = "Grok(xAI)"
    category = CATEGORY_CUSTOM
    credential_schema = []  # 读本地 ~/.grok/auth.json,无需用户输入

    def _read_token(self) -> Tuple[Optional[str], Optional[str]]:
        """从 ~/.grok/auth.json 选 token:OIDC(SuperGrok)优先,legacy 兜底。"""
        data = QuotaProvider.read_cli_json(".grok", "auth.json")
        if not isinstance(data, dict):
            return None, "未找到 ~/.grok/auth.json(请先用 Grok CLI 登录)"
        oidc = legacy = None
        for scope, entry in data.items():
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            if scope.startswith(OIDC_SCOPE_PREFIX):
                oidc = entry
            elif scope == LEGACY_SESSION_SCOPE or "/sign-in" in scope:
                legacy = entry
        entry = oidc or legacy
        if not entry:
            return None, "auth.json 里没有可用 token"
        return entry.get("key"), None

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        token, msg = self._read_token()
        if not token:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="not_found", error_message=msg)

        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/?_s=usage",
            "Accept": "*/*",
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "User-Agent": "AI额度悬浮窗",
        }
        try:
            resp = self._session.post(GROK_BILLING_ENDPOINT, data=b"\x00" * 5,
                                      headers=headers, timeout=15)
        except requests.exceptions.Timeout as e:
            raise TransientFetchError("Grok 请求超时") from None
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            raise TransientFetchError("Grok 网络错误，请检查代理和网络") from None

        status = resp.status_code
        if status in (401, 403):
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="expired", error_message="token 失效,请重新登录 Grok CLI")
        if not (200 <= status < 300):
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message=f"HTTP {status}")

        # gRPC 错误不走 HTTP 状态码:先查响应头(trailers-only 响应),再查响应体 trailer 帧。
        # 不识别的话 token 失效(status 16)会被误报成"解析失败",用户不知道要重新登录。
        grpc_status = None
        grpc_message = ""
        hs = resp.headers.get("grpc-status")
        if hs is not None:
            try:
                grpc_status = int(hs)
                grpc_message = resp.headers.get("grpc-message", "")
            except ValueError:
                pass
        if grpc_status is None:
            trailers = _grpc_web_trailer_fields(resp.content)
            if trailers.get("grpc-status") is not None:
                try:
                    grpc_status = int(trailers["grpc-status"])
                    grpc_message = trailers.get("grpc-message", "")
                except ValueError:
                    pass
        if grpc_status is not None and grpc_status != 0:
            if _is_transient_grpc_status(grpc_status, grpc_message):
                raise TransientFetchError(
                    f"Grok gRPC 瞬时失败 (grpc-status {grpc_status})")
            if _is_grpc_auth_failure(grpc_status, grpc_message):
                return QuotaSnapshot.failure_snapshot(
                    self.provider_id, credential_status="expired",
                    error_message="token 失效,请重新登录 Grok CLI "
                                  f"(grpc-status {grpc_status})")
            if _is_team_billing_unavailable(grpc_status, grpc_message):
                return QuotaSnapshot.failure_snapshot(
                    self.provider_id, credential_status="parse_error",
                    error_message="Grok 团队主体暂不支持用量查询(No personal team)")
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error",
                error_message=f"Grok gRPC 失败 (grpc-status {grpc_status})")

        now_secs = int(time.time())
        try:
            used, reset = _parse_billing_payload(resp.content, now_secs)
        except ProviderError as e:
            logger.warning("[grok] 解析失败: %s", e)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error", error_message=str(e))

        name = _tier_name_for_reset(reset, now_secs)
        resets_at = format_reset_time_seconds(reset) if reset else ""
        tier = QuotaTier(name=name, kind=KIND_PERCENT,
                         utilization=max(0.0, min(100.0, used)), resets_at=resets_at)
        return QuotaSnapshot.success_snapshot(self.provider_id, [tier],
                                              credential_message="SuperGrok")
