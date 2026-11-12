"""GLM 国际版(z.ai)—— 和 GLM 个人版逻辑完全一样,只是换域名。"""
from api.providers.glm import GLMProvider
from api.registry import register


@register
class GLMInternationalProvider(GLMProvider):
    provider_id = "glm_en"
    display_name = "GLM 国际版(z.ai)"
    # 国际版域名是 api.z.ai,其余(原始 key 鉴权、解析)都继承 GLMProvider
    DEFAULT_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
