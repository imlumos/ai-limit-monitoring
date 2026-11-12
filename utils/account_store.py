"""
多账号存储 (account_store)
===========================

【这个文件做什么】
管理"多个账号"的持久化。一个账号 = 一个平台的凭据(如一个 GLM API Key)。
替代旧的 utils/session_manager.py(它只能存单个 Key)。

【数据存哪里】(沿用旧项目的分工)
  - data/accounts.json:账号"非敏感"信息(id、标签、平台、是否启用、非密凭据)。
        明文 JSON,便于查看/备份。
  - 系统 keyring:每个账号每个"敏感"字段(API Key、AK/SK)一条,密文存储。
        keyring scheme:service="glm_monitor"(沿用旧值;项目已改名 AI额度悬浮窗,
        但服务名涉及凭据管理器里的存量数据,严禁改,否则所有已存 Key 全部失联)、
        username="account:<账号id>:<字段key>"。
  这样即使 accounts.json 被拷走,没有系统登录也拿不到 API Key。

【关键概念:为什么 secrets 不进 JSON】
JSON 明文,任何能读到文件的程序都能看;系统凭据库由 OS 加密,只有当前用户
登录后才能解密。所以敏感数据一律走 keyring,JSON 只存开关和标签。

【迁移(不丢数据)】
迁移仅执行一次。新凭据和账号保存成功后清理旧条目。
凭据操作失败时保留停用账号供重试或删除,不会继续轮询。
"""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict, replace
from typing import Optional, List, Dict

import keyring

from utils.paths import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)


@dataclass
class Account:
    """
    一个账号(非敏感部分)。敏感凭据存 keyring,不在这里。

    【id】稳定唯一,uuid4 十六进制,创建后不变(keyring 条目用它作 key)。
    【label】用户起的名字,如 "工作号"、"测试号"。
    【provider_id】平台标识,如 "glm"。
    【enabled】是否参与轮询(True 才取额度)。
    【base_url_override】可选自定义端点(ZenMux 等需要用户填 base_url)。
    【non_secret_creds】非敏感凭据(GLM 团队的 org/project id),存 JSON。
    【created_at】创建时间(epoch 毫秒)。
    """
    id: str
    label: str
    provider_id: str
    enabled: bool = True
    base_url_override: Optional[str] = None
    non_secret_creds: Dict[str, str] = field(default_factory=dict)
    created_at: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(
            id=d["id"],
            label=d.get("label", ""),
            provider_id=d.get("provider_id", ""),
            enabled=d.get("enabled", True),
            base_url_override=d.get("base_url_override"),
            non_secret_creds=d.get("non_secret_creds", {}) or {},
            created_at=d.get("created_at", 0),
        )


# keyring 服务名沿用旧值,所有条目都归在它下面(Windows 凭据管理器里成组)
SERVICE_NAME = "glm_monitor"
ACCOUNTS_FILE = "accounts.json"


class AccountStoreError(RuntimeError):
    """可安全展示给用户的存储错误,不含底层异常或凭据值。"""


class AccountStore:
    """JSON 与系统凭据库不能原子提交,写入前先持久化停用记录。"""

    def __init__(self):
        self._accounts: List[Account] = []
        self._migration_done = False
        self._load()

    def _file_path(self) -> str:
        return get_data_path(ACCOUNTS_FILE)

    def _load(self) -> None:
        path = self._file_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._migration_done = bool(raw.get("migration_done", False))
                self._accounts = [Account.from_dict(a) for a in raw.get("accounts", [])]
            except Exception as e:
                logger.error("账号文件读取失败(%s)", type(e).__name__)
                raise AccountStoreError("账号文件无法读取，请检查文件和权限；未覆盖原文件") from None
        if not self._migration_done:
            self._migrate_legacy_single_key()
            self._migration_done = True
            try:
                self._save()
            except AccountStoreError:
                self._migration_done = False
                raise

    def _save(self) -> None:
        payload = {
            "version": 1,
            "migration_done": self._migration_done,
            "accounts": [a.to_dict() for a in self._accounts],
        }
        path = self._file_path()
        tmp = path + ".tmp"
        try:
            ensure_data_dir()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.error("账号文件保存失败(%s)", type(e).__name__)
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise AccountStoreError("账号文件保存失败，请检查目录权限和磁盘空间") from None

    def _commit_accounts(self, accounts: List[Account]) -> None:
        previous = self._accounts
        self._accounts = accounts
        try:
            self._save()
        except AccountStoreError:
            self._accounts = previous
            raise

    def _replace_account(self, account: Account) -> None:
        self._commit_accounts([account if a.id == account.id else a for a in self._accounts])

    def _migrate_legacy_single_key(self) -> None:
        legacy_key = self._read_secret("apikey")
        if not legacy_key:
            return
        account = self.get_account("migrated-glm")
        if account is None:
            account = Account(id="migrated-glm", label="GLM", provider_id="glm",
                              enabled=False, created_at=int(time.time() * 1000))
            self._commit_accounts(self._accounts + [account])
        else:
            account = replace(account, enabled=False)
            self._replace_account(account)
        self._set_keyring(account.id, "api_key", legacy_key)
        self._replace_account(replace(account, enabled=True))
        # 新条目与 JSON 都成功后才删除旧凭据;失败会上报,下次启动可重试。
        self._delete_secret("apikey")
        logger.info("旧版 GLM 凭据迁移完成")

    @staticmethod
    def _kr_username(account_id: str, field_key: str) -> str:
        return f"account:{account_id}:{field_key}"

    @staticmethod
    def _read_secret(username: str) -> Optional[str]:
        try:
            return keyring.get_password(SERVICE_NAME, username)
        except Exception as e:
            logger.error("系统凭据读取失败(%s)", type(e).__name__)
            raise AccountStoreError("无法读取系统凭据，请检查凭据管理器后重试") from None

    def _set_keyring(self, account_id: str, field_key: str, value: str) -> None:
        username = self._kr_username(account_id, field_key)
        try:
            keyring.set_password(SERVICE_NAME, username, value)
            if keyring.get_password(SERVICE_NAME, username) != value:
                raise RuntimeError("credential readback mismatch")
        except Exception as e:
            logger.error("系统凭据保存失败(%s)", type(e).__name__)
            raise AccountStoreError("凭据保存未完成；账号已停用，可重试或删除记录后重新添加") from None

    def _get_keyring(self, account_id: str, field_key: str) -> Optional[str]:
        return self._read_secret(self._kr_username(account_id, field_key))

    def _delete_secret(self, username: str) -> None:
        if self._read_secret(username) is None:
            return
        try:
            keyring.delete_password(SERVICE_NAME, username)
            if keyring.get_password(SERVICE_NAME, username) is not None:
                raise RuntimeError("credential still present")
        except Exception as e:
            logger.error("系统凭据删除失败(%s)", type(e).__name__)
            raise AccountStoreError("凭据删除未完成；已保留停用记录，请重试删除") from None

    def _del_keyring(self, account_id: str, field_key: str) -> None:
        self._delete_secret(self._kr_username(account_id, field_key))

    def list_accounts(self) -> List[Account]:
        return list(self._accounts)

    def list_enabled(self) -> List[Account]:
        return [a for a in self._accounts if a.enabled]

    def get_account(self, account_id: str) -> Optional[Account]:
        return next((a for a in self._accounts if a.id == account_id), None)

    @staticmethod
    def _schema_fields(provider_id: str):
        from api.registry import PROVIDER_REGISTRY
        cls = PROVIDER_REGISTRY.get(provider_id)
        if cls is None:
            raise AccountStoreError("平台未注册，拒绝保存或删除未知凭据字段")
        return cls.credential_schema

    @classmethod
    def _validate_credentials(cls, provider_id, credentials, base_url_override=None):
        fields = cls._schema_fields(provider_id)
        if provider_id == "zenmux" or base_url_override:
            raise AccountStoreError("试用版已停用自定义端点和 ZenMux")
        if set(credentials) - {f.key for f in fields}:
            raise AccountStoreError("凭据包含未知字段，拒绝明文保存")
        for f in fields:
            value = credentials.get(f.key)
            if f.required and not value:
                raise AccountStoreError("缺少必填凭据字段")
            if value is not None and (not isinstance(value, str) or any(c in value for c in "\r\n\0")):
                raise AccountStoreError("凭据格式无效，请重新输入")
        return fields

    def _write_credentials(self, account_id, fields, credentials):
        # 先写所有新值,成功后才删除不再使用的可选字段。
        for f in fields:
            if f.secret and credentials.get(f.key) is not None:
                self._set_keyring(account_id, f.key, credentials[f.key])
        for f in fields:
            if f.secret and credentials.get(f.key) is None:
                self._del_keyring(account_id, f.key)

    def add_account(self, provider_id: str, label: str, credentials: Dict[str, str],
                    *, base_url_override: Optional[str] = None) -> Account:
        fields = self._validate_credentials(provider_id, credentials, base_url_override)
        account_id = uuid.uuid4().hex
        while self.get_account(account_id):
            account_id = uuid.uuid4().hex
        non_secret = {f.key: credentials[f.key] for f in fields
                      if not f.secret and credentials.get(f.key) is not None}
        account = Account(id=account_id, label=label or provider_id, provider_id=provider_id,
                          enabled=False, non_secret_creds=non_secret,
                          created_at=int(time.time() * 1000))
        # 先留下可追踪的停用记录,即使进程中断也不会产生无主凭据。
        self._commit_accounts(self._accounts + [account])
        self._write_credentials(account_id, fields, credentials)
        account = replace(account, enabled=True)
        self._replace_account(account)
        return account

    def update_account(self, account_id: str, *, label: Optional[str] = None,
                       credentials: Optional[Dict[str, str]] = None,
                       base_url_override: Optional[str] = None,
                       enabled: Optional[bool] = None) -> Optional[Account]:
        account = self.get_account(account_id)
        if account is None:
            return None
        if base_url_override:
            raise AccountStoreError("试用版已停用自定义端点")
        target = replace(account,
                         label=account.label if label is None else label,
                         enabled=account.enabled if enabled is None else enabled,
                         base_url_override=(account.base_url_override
                                            if base_url_override is None else None))
        if credentials is not None:
            fields = self._validate_credentials(account.provider_id, credentials, target.base_url_override)
            self._replace_account(replace(account, enabled=False))
            self._write_credentials(account_id, fields, credentials)
            target.non_secret_creds = {f.key: credentials[f.key] for f in fields
                                       if not f.secret and credentials.get(f.key) is not None}
        elif enabled:
            self._validate_credentials(account.provider_id, self.get_credentials(account), target.base_url_override)
        self._replace_account(target)
        return target

    def reorder_accounts(self, account_ids: List[str]) -> None:
        """按给定 id 顺序重排账号(必须恰好是现有账号的一个排列,否则拒绝)。"""
        by_id = {a.id: a for a in self._accounts}
        if sorted(account_ids) != sorted(by_id):
            raise AccountStoreError("账号列表已变化，请重试排序")
        self._commit_accounts([by_id[i] for i in account_ids])

    def delete_account(self, account_id: str) -> None:
        account = self.get_account(account_id)
        if account is None:
            return
        self._replace_account(replace(account, enabled=False))
        if account_id == "migrated-glm":
            self._delete_secret("apikey")
        for f in self._schema_fields(account.provider_id):
            if f.secret:
                self._del_keyring(account_id, f.key)
        self._commit_accounts([a for a in self._accounts if a.id != account_id])

    def get_credentials(self, account: Account) -> Dict[str, str]:
        creds = {}
        for f in self._schema_fields(account.provider_id):
            value = (self._get_keyring(account.id, f.key) if f.secret
                     else account.non_secret_creds.get(f.key))
            if value is not None:
                creds[f.key] = value
        return creds
