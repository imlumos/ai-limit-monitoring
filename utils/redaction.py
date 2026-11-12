"""日志最后一道脱敏保护;业务层仍应避免记录原始响应。"""
import logging
import re
from urllib.parse import urlsplit


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AUTH = re.compile(r"(?i)((?:authorization|cookie|set-cookie)[\"']?\s*[:=]\s*[\"']?)[^\r\n]+")
_SECRET = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"token|password|secret[_-]?access[_-]?key|client[_-]?secret|cookie|set-cookie)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(text: str) -> str:
    def safe_url(match):
        try:
            url = urlsplit(match.group())
            # 路径也可能包含秘密,日志只保留协议和主机。
            return f"{url.scheme}://{url.hostname or '[redacted]'}/[redacted]"
        except ValueError:
            return "[redacted-url]"
    text = _URL.sub(safe_url, str(text))
    text = _AUTH.sub(r"\1[redacted]", text)
    text = _SECRET.sub(r"\1[redacted]", text)
    text = _BEARER.sub(r"\1 [redacted]", text)
    return _EMAIL.sub("[redacted-email]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        # 格式化之后再过滤,同时覆盖插值参数、异常堆栈和异常链。
        return redact(super().format(record))
