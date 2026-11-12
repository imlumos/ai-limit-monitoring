"""GitHub Copilot —— 用户填 GitHub OAuth token(无 CLI 凭据文件可读)。

【说明】cc-switch 跑自己的 device-flow OAuth 存 token;这里简化为让用户填一个
GitHub OAuth token(gho_...,在 github.com/settings/tokens 生成,勾 read:user 即可)。
【接口】GET api.github.com/copilot_internal/user,
  Authorization: token {github_token}(注意是 "token " 不是 "Bearer ")。
【解析】只取 quota_snapshots.premium_interactions:
  used=entitlement-remaining, total=entitlement, remaining, unit=requests。
  档名用 TIER_CREDITS("额度")——它是月度次数配额,不是钱包余额;
  format_money 对 unit=requests 按次数整数显示。
"""
import logging
from typing import List, Optional, Tuple, Dict

from api.models import QuotaTier, KIND_MONEY, QuotaSnapshot, TIER_CREDITS
from api.provider_base import QuotaProvider, CredentialField, CATEGORY_MONEY
from api.registry import register
from api._http import get_json, TransientFetchError, DeterministicError

logger = logging.getLogger(__name__)


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


@register
class CopilotProvider(QuotaProvider):
    provider_id = "copilot"
    display_name = "GitHub Copilot"
    category = CATEGORY_MONEY
    credential_schema = [
        CredentialField("github_token", "GitHub Token", secret=True,
                        placeholder="gho_...(OAuth token)"),
    ]

    def fetch(self, credentials: Dict[str, str], *, base_url=None) -> QuotaSnapshot:
        token = credentials.get("github_token", "")
        if not token:
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="not_found",
                error_message="请填写 GitHub Token")
        headers = {
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "editor-version": "vscode/1.110.1",
            "editor-plugin-version": "copilot-chat/0.38.2",
            "user-agent": "GitHubCopilotChat/0.38.2",
            "x-github-api-version": "2025-10-01",
        }
        try:
            body, _status = get_json(self._session,
                                     "https://api.github.com/copilot_internal/user", headers)
        except TransientFetchError:
            raise
        except DeterministicError as e:
            return self._failure_from_deterministic(e)

        snap = (body.get("quota_snapshots") or {}).get("premium_interactions") or {}
        ent = _i(snap.get("entitlement"))
        rem = _i(snap.get("remaining"))
        used = (ent - rem) if (ent is not None and rem is not None) else None
        plan = body.get("copilot_plan")
        reset = body.get("quota_reset_date")
        message = None
        if plan or reset:
            message = f"{plan} · 重置 {reset}" if (plan and reset) else (plan or f"重置 {reset}")
        tier = QuotaTier(
            name=TIER_CREDITS, kind=KIND_MONEY,
            total=float(ent) if ent is not None else None,
            used=float(used) if used is not None else None,
            remaining=float(rem) if rem is not None else None,
            unit="requests",
            is_valid=(rem is None or rem > 0))
        return QuotaSnapshot.success_snapshot(self.provider_id, [tier], credential_message=message)
