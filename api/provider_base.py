"""
Provider 抽象基类 (provider_base)
=================================

【这个文件做什么】
定义"平台额度提供者"的统一接口 QuotaProvider,以及两个常用基类:
  - BearerKeyProvider:用 "Authorization: Bearer <key>" 的平台(绝大多数)
  - RawKeyProvider:    用 "Authorization: <原始 key>" 的平台(GLM,不带 Bearer)

【为什么要这层抽象】
原来只有一个写死的 GLMClient,要加新平台就得复制粘贴。
抽出 QuotaProvider 后,每个平台写一个 ~60 行的子类即可,UI 和轮询层只认接口、
不关心是哪个平台——这就是"开闭原则":加平台不改老代码。

【credential_schema 是关键】
每个 provider 声明自己需要哪些凭据字段(用 CredentialField 描述),
"添加账号"表单据此自动生成输入框:GLM 显示 1 个框、GLM 团队 3 个、火山 2 个。
UI 代码完全不用 if/else 分平台。

【模板方法模式】
SimpleGetJsonProvider 把"发请求→解析→包成快照→处理错误"的公共流程写死,
子类只填 4 个钩子:_url / _auth_value / _parse(必填)、_extra_headers / _params(可选)。
这样所有 GET-JSON 类平台的错误处理只写一次(和 cc-switch 一致)。
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional, List, Tuple, Dict

from api._http import get_json
from api.transport import ProviderSession
from api.models import (QuotaSnapshot, QuotaTier, DeterministicError,
                        TransientFetchError)

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """
    provider 在解析阶段判定的"确定性失败",如业务码非成功(GLM 的 code!=200、
    MiniMax 的 base_resp.status_code 非 0)。

    【和 DeterministicError 的区别】
    DeterministicError 是 HTTP 层的(非 2xx / JSON 解析失败);
    ProviderError 是业务层的(HTTP 200、JSON 合法,但业务语义表示失败)。
    provider 的 _parse 里判定业务失败时抛它,模板会转成失败快照。
    """
    def __init__(self, message: str, *, credential_status: str = "parse_error"):
        super().__init__(message)
        self.credential_status = credential_status


class CredentialField:
    """
    一个凭据字段的描述(驱动"添加账号"表单自动生成)。

    【字段含义】
      key:         存储用的字段名,如 "api_key" / "organization_id"
      label:       表单上显示的中文标签
      secret:      True → 存进系统凭据库 keyring(密文);False → 存 accounts.json(明文)
      required:    是否必填
      placeholder: 输入框占位提示
    """
    def __init__(self, key: str, label: str, *, secret: bool = True,
                 required: bool = True, placeholder: str = ""):
        self.key = key
        self.label = label
        self.secret = secret
        self.required = required
        self.placeholder = placeholder


class QuotaProvider(ABC):
    """
    所有平台 Provider 的抽象基类。

    子类必须设置类属性:
      provider_id:       唯一标识,如 "glm"、"deepseek"
      display_name:      显示名,如 "GLM(智谱)"
      category:          归类,见 CATEGORY_* 常量
      credential_schema: 该平台需要的凭据字段列表(List[CredentialField])
    并实现 fetch()。
    """
    provider_id: str = ""
    display_name: str = ""
    category: str = ""
    credential_schema: List[CredentialField] = []

    def __init__(self):
        # 每次采集持有自己的 provider,会话不在账号之间共享。
        self._session = ProviderSession(self.provider_id)

    def close(self):
        self._session.close()

    @staticmethod
    def read_cli_json(*parts) -> Optional[dict]:
        """读取用户主目录下的 CLI 凭据 JSON。

        Path.home() 在 Windows 下是 %USERPROFILE%,macOS/Linux 是 ~。
        如 read_cli_json(".claude", ".credentials.json") → ~/.claude/.credentials.json。
        文件不存在/解析失败返回 None。
        """
        from pathlib import Path
        try:
            p = Path.home().joinpath(*parts)
            with open(p, "r", encoding="utf-8") as f:
                import json
                return json.load(f)
        except Exception:
            return None

    @abstractmethod
    def fetch(self, credentials: Dict[str, str], *, base_url: Optional[str] = None) -> QuotaSnapshot:
        """
        拉取额度。

        Args:
            credentials: 凭据字典,{field_key: value},至少含 credential_schema 声明的字段
            base_url:    可选端点覆盖(ZenMux 等需要用户填 base_url 时用)

        Returns:
            QuotaSnapshot(account_id / queried_at 留空,由轮询层填)

        Raises:
            TransientFetchError: 瞬时错误(网络/超时),交给轮询层保留上次好值
        """

    def _failure_from_deterministic(self, exc: DeterministicError) -> QuotaSnapshot:
        """把确定性 HTTP 错误转成失败快照:401/403 当 Key 失效,其余当解析/业务错误。"""
        if exc.status_code in (401, 403):
            logger.warning("[%s] 鉴权失败(HTTP %s)", self.provider_id, exc.status_code)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="expired",
                error_message="鉴权失败:Key 无效或已失效")
        logger.warning("[%s] 请求失败:%s", self.provider_id, exc)
        return QuotaSnapshot.failure_snapshot(
            self.provider_id, credential_status="parse_error",
            error_message=str(exc))


# ====== 平台归类常量(给 UI 分组/选图标用) ======
CATEGORY_PERCENT = "percent"      # 配额百分比类(GLM/Kimi/MiniMax/ZenMux)
CATEGORY_MONEY = "money"          # 余额钱包类(DeepSeek/OpenRouter 等)
CATEGORY_OAUTH_CLI = "oauth_cli"  # 官方 CLI OAuth(Claude/Codex/Gemini)
CATEGORY_CUSTOM = "custom"        # 自定义鉴权(火山/Grok)


class SimpleGetJsonProvider(QuotaProvider):
    """
    GET 一个 JSON 接口的模板基类(覆盖绝大多数平台:DeepSeek/Kimi/MiniMax/GLM 等)。

    【模板流程】(写死,子类不用管)
        1. 拼鉴权头 + 额外头
        2. get_json 发请求(瞬时错误直接抛、确定性错误转失败快照)
        3. 子类的 _parse 把响应体解析成档位列表
        4. 包成成功快照返回

    【子类要实现的钩子】
        _url(base_url, credentials) -> str            接口地址
        _auth_value(api_key) -> str                    Authorization 头的值
        _parse(body, credentials) -> (tiers, message)  解析响应体
    【可选钩子】
        _extra_headers(credentials) -> dict   额外请求头(GLM 团队的 org/project)
        _params(credentials) -> dict|None     URL 查询参数(GLM 团队的 ?type=2)
    """
    def _auth_value(self, api_key: str) -> str:
        raise NotImplementedError

    def _url(self, base_url: Optional[str], credentials: Dict[str, str]) -> str:
        raise NotImplementedError

    def _extra_headers(self, credentials: Dict[str, str]) -> Dict[str, str]:
        return {}

    def _params(self, credentials: Dict[str, str]) -> Optional[dict]:
        return None

    def _parse(self, body, credentials: Dict[str, str]) -> Tuple[List[QuotaTier], Optional[str]]:
        raise NotImplementedError

    def fetch(self, credentials: Dict[str, str], *, base_url: Optional[str] = None) -> QuotaSnapshot:
        if base_url or self.provider_id == "zenmux":
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error",
                error_message="试用版已停用自定义端点和 ZenMux，请使用平台默认地址")
        api_key = credentials.get("api_key", "")
        url = self._url(base_url, credentials)
        logger.debug("[%s] 查询额度", self.provider_id)
        # 基础头与原 GLMClient 保持一致;Authorization 由子类决定带不带 Bearer
        headers = {
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
            "Authorization": self._auth_value(api_key),
        }
        headers.update(self._extra_headers(credentials))

        try:
            body, _status = get_json(self._session, url, headers,
                                     params=self._params(credentials))
        except TransientFetchError as e:
            logger.debug("[%s] 瞬时错误(保留上次值)", self.provider_id)
            raise  # 瞬时错误:原样上抛,轮询层保留上次好值
        except DeterministicError as e:
            return self._failure_from_deterministic(e)

        try:
            tiers, message = self._parse(body, credentials)
            if not tiers:
                raise ProviderError("未解析到有效额度，请核对账号和平台接口")
            logger.info("[%s] 取额度成功:%d 档", self.provider_id, len(tiers))
            return QuotaSnapshot.success_snapshot(self.provider_id, tiers,
                                                  credential_message=message)
        except ProviderError as e:
            # 业务层确定性失败(HTTP 200 但业务码非成功)
            logger.warning("%s 业务失败", self.provider_id)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status=e.credential_status,
                error_message="平台返回业务错误，请核对凭据、权限和套餐状态")
        except Exception as e:
            logger.warning("%s 解析响应失败(%s)", self.provider_id, type(e).__name__)
            return QuotaSnapshot.failure_snapshot(
                self.provider_id, credential_status="parse_error",
                error_message="额度响应解析失败，请核对平台接口")


class BearerKeyProvider(SimpleGetJsonProvider):
    """Authorization: Bearer <key>(DeepSeek/StepFun/硅基/OpenRouter/Novita/Kimi/MiniMax/ZenMux)。"""
    def _auth_value(self, api_key: str) -> str:
        return f"Bearer {api_key}"


class RawKeyProvider(SimpleGetJsonProvider):
    """Authorization: <原始 key>,不带 Bearer(GLM / GLM 国际)。"""
    def _auth_value(self, api_key: str) -> str:
        return api_key
