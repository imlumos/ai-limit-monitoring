"""GLM 团队版—— 个人版接口加 ?type=2 + 组织/项目请求头,解析相同。"""
from typing import Dict, Optional

from api.providers.glm import GLMProvider
from api.provider_base import CredentialField
from api.registry import register


@register
class GLMTeamProvider(GLMProvider):
    provider_id = "glm_team"
    display_name = "GLM 团队版"
    # 团队版需要额外两个 ID(在开放平台团队控制台拿),非敏感 → 存 accounts.json
    credential_schema = [
        CredentialField("api_key", "API Key", secret=True,
                        placeholder="团队 API Key"),
        CredentialField("organization_id", "组织 ID", secret=False, required=True,
                        placeholder="bigmodel-organization"),
        CredentialField("project_id", "项目 ID", secret=False, required=True,
                        placeholder="bigmodel-project"),
    ]

    def _params(self, credentials: Dict[str, str]) -> Optional[dict]:
        # 团队版用 ?type=2 查询
        return {"type": 2}

    def _extra_headers(self, credentials: Dict[str, str]) -> Dict[str, str]:
        h = {}
        org = credentials.get("organization_id")
        proj = credentials.get("project_id")
        if org:
            h["bigmodel-organization"] = org
        if proj:
            h["bigmodel-project"] = proj
        return h
