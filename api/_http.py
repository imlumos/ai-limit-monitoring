"""
共享 HTTP 工具 (_http)
======================

【这个文件做什么】
只提供一个函数 get_json:发 GET 请求、把响应体当 JSON 解析、按 cc-switch 的
"瞬时 vs 确定性"错误纪律把异常分类。所有 provider 都复用它,错误处理只写一次。

【为什么要单独抽这个】
原 glm_client.py 直接用 response.json() 解析,有两个问题:
  1. 它把"网络断了/超时"和"返回的不是 JSON"都笼统地当失败,UI 没法区分——
     网络抖动时本来该保留上次的好值,结果被清空了。
  2. requests 的 response.json() 在"响应体被截断"和"真的不是 JSON"时
     都抛同一种 JSONDecodeError,无法区分。
解决办法:先 response.content 拿到原始字节,再 json.loads,并在过程中
把网络/超时/读中断归为 TransientFetchError(瞬时),把非 2xx、JSON 解析失败
归为 DeterministicError(确定性)。这正是 cc-switch 用 bytes() 再 from_slice 的用意。

【关键概念:requests 的异常体系】
  requests.exceptions.Timeout            请求超时
  requests.exceptions.ConnectionError    连接失败(DNS、拒绝、断网)
  requests.exceptions.ChunkedEncodingError 响应流读到一半中断
这三类都是"瞬时"的——重试可能就好。

【返回值】
  (data, status_code):data 是解析好的 dict/list,status_code 是 HTTP 状态码。
  出错时不返回,而是抛 TransientFetchError 或 DeterministicError。
"""
import json
import logging
from typing import Optional, Tuple, Any

import requests

from api.models import TransientFetchError, DeterministicError

logger = logging.getLogger(__name__)

# 429 = 服务端限流(如 Claude 的 oauth/usage 与 Claude Code 共用 token 计数),过一会自动恢复
_RATE_LIMITED_MSG = "请求过于频繁(HTTP 429),稍后自动重试"


def _decode(raw: bytes, response: requests.Response) -> str:
    """把响应字节按声明编码解码成字符串(拿不到编码就 utf-8 容错)。"""
    encoding = response.encoding or "utf-8"
    return raw.decode(encoding, errors="replace")


def get_json(
    session: requests.Session,
    url: str,
    headers: dict,
    *,
    timeout: float = 15.0,
    params: Optional[dict] = None,
) -> Tuple[Any, int]:
    """
    发 GET 请求并解析 JSON。

    Args:
        session: requests.Session(复用连接池,调用方传入)
        url:     请求地址
        headers: 请求头(已含鉴权)
        timeout: 超时秒数,默认 15
        params:  URL 查询参数(可选)

    Returns:
        (data, status_code)

    Raises:
        TransientFetchError:   网络/超时/响应体不完整 → 调用方应保留上次好值
        DeterministicError:    非 2xx 或 JSON 解析失败 → 调用方应显示错误
    """
    # ---- 1. 发请求(连接/超时/读中断都在这一步可能抛) ----
    try:
        response = session.get(url, headers=headers, timeout=timeout, params=params)
    except requests.exceptions.Timeout as e:
        raise TransientFetchError("请求超时，请稍后重试") from None
    except (requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError) as e:
        raise TransientFetchError("网络连接失败，请检查网络和代理") from None

    status = response.status_code

    # ---- 2. 读原始字节(注意:requests 默认已读完,这里再取一次并校验完整性) ----
    try:
        raw = response.content
    except (requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError) as e:
        # 理论上 .get 已经读完,防御性兜底
        raise TransientFetchError("响应体读取中断") from None

    # 校验完整性:声明的 Content-Length 大于实际收到的 → 传输被截断(瞬时)
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if len(raw) < int(declared):
                raise TransientFetchError(
                    f"响应体不完整({len(raw)}/{declared} 字节)")
        except ValueError:
            pass  # Content-Length 不是数字,忽略

    # ---- 3. 429 限流 → 瞬时(保留上次好值);其余非 2xx → 确定性错误(调用方据此判断 401/403 等) ----
    if status == 429:
        raise TransientFetchError(_RATE_LIMITED_MSG)
    if not (200 <= status < 300):
        snippet = _decode(raw, response)[:500]
        raise DeterministicError(status, f"HTTP {status}", snippet)

    # ---- 4. 解析 JSON:失败归为确定性错误 ----
    text = _decode(raw, response)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise DeterministicError(status, "JSON 解析失败", text[:500]) from None

    return data, status


def post_json(
    session: requests.Session,
    url: str,
    headers: dict,
    body: Optional[Any] = None,
    *,
    timeout: float = 15.0,
    extra_headers: Optional[dict] = None,
    raw_body: Optional[bytes] = None,
) -> Tuple[Any, int]:
    """
    发 POST 请求并解析 JSON(给 Gemini 两步 cloudcode 调用、Grok grpc-web 用)。

    Args:
        body:     请求体,会被 json.dumps(若非 None)。
        raw_body: 原始字节请求体(与 body 二选一;Grok 的 5 字节帧用)。
        extra_headers: 追加头(grpc-web 的 x-grpc-web 等)。

    错误分类纪律与 get_json 一致(瞬时/确定性)。
    """
    data_bytes: bytes = b""
    if raw_body is not None:
        data_bytes = raw_body
    elif body is not None:
        data_bytes = json.dumps(body).encode("utf-8")

    merged = dict(headers)
    if extra_headers:
        merged.update(extra_headers)

    try:
        response = session.post(url, data=data_bytes, headers=merged, timeout=timeout)
    except requests.exceptions.Timeout as e:
        raise TransientFetchError("请求超时，请稍后重试") from None
    except (requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError) as e:
        raise TransientFetchError("网络连接失败，请检查网络和代理") from None

    status = response.status_code
    try:
        raw = response.content
    except (requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError) as e:
        raise TransientFetchError("响应体读取中断") from None

    if status == 429:
        raise TransientFetchError(_RATE_LIMITED_MSG)
    if not (200 <= status < 300):
        snippet = _decode(raw, response)[:500]
        raise DeterministicError(status, f"HTTP {status}", snippet)

    text = _decode(raw, response)
    try:
        return json.loads(text), status
    except (json.JSONDecodeError, ValueError):
        # 调用方可能期望原始文本(如 grpc-web),这里给个兜底
        return {"_raw_text": text}, status
