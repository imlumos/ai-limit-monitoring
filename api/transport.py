"""额度请求的固定端点策略。自定义端点在个人试用版中停用。"""
from urllib.parse import urlsplit

import requests

from api.models import DeterministicError
from utils.proxy_config import get_proxy_settings


ALLOWED_HOSTS = {
    "glm": {"open.bigmodel.cn"},
    "glm_en": {"api.z.ai"},
    "glm_team": {"open.bigmodel.cn"},
    "kimi": {"api.kimi.com"},
    "minimax": {"api.minimaxi.com"},
    "deepseek": {"api.deepseek.com"},
    "stepfun": {"api.stepfun.com"},
    "siliconflow": {"api.siliconflow.cn"},
    "openrouter": {"openrouter.ai"},
    "novita": {"api.novita.ai"},
    "claude": {"api.anthropic.com"},
    "codex": {"chatgpt.com"},
    "gemini": {"oauth2.googleapis.com", "cloudcode-pa.googleapis.com"},
    "copilot": {"api.github.com"},
    "volcengine": {"open.volcengineapi.com"},
    "grok": {"grok.com"},
    "cursor": {"cursor.com"},
}


def validate_endpoint(provider_id: str, url: str) -> None:
    try:
        parsed = urlsplit(url)
        allowed = (parsed.scheme == "https"
                   and parsed.hostname in ALLOWED_HOSTS.get(provider_id, set())
                   and parsed.port in (None, 443)
                   and parsed.username is None and parsed.password is None
                   and not parsed.fragment)
    except (TypeError, ValueError):
        allowed = False
    if not allowed:
        # 不回显 URL,其中可能带用户误填的密钥。
        raise DeterministicError(None, "已阻止请求：试用版只允许平台固定的 HTTPS 端点")


class ProviderSession(requests.Session):
    def __init__(self, provider_id: str):
        super().__init__()
        self.provider_id = provider_id
        proxy = get_proxy_settings()
        # 显式配置/直连不被终端或系统代理覆盖。environment 保持原行为。
        self.trust_env = proxy.mode == "environment"
        if proxy.mode == "custom":
            self.proxies = {"http": proxy.url, "https": proxy.url}

    def request(self, method, url, **kwargs):
        validate_endpoint(self.provider_id, url)
        # 不跟随跳转,包括会把刷新令牌 POST 正文转发出去的 307/308。
        kwargs["allow_redirects"] = False
        kwargs["verify"] = True
        return super().request(method, url, **kwargs)
