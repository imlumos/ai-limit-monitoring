"""
Session 管理器 - 使用 keyring 持久化 API Key
=============================================

【这个文件做什么】
把用户的 API Key 保存到磁盘,下次启动时自动读取,实现"记住 API Key"功能。
- 是否记住 -> 明文 JSON 文件 data/session.json 里的 {remember_api_key: bool}
- API Key 本体 -> 加密存储在系统凭据库(Windows Credential Manager / macOS Keychain)
  通过第三方库 keyring 访问,代码里不需要管加密细节。

(老版本存的是"账号 + 密码",改成 API Key 方式后,账号密码相关逻辑已全部移除。)

【为什么不把 API Key 也存 JSON?】
JSON 是明文,任何能读到该文件的程序都能看到,不安全。
系统凭据库由操作系统加密保护,只有当前用户登录后才能解密,
所以敏感数据(API Key、Token、密码)都应该走 keyring。

【设计模式:全部用 @staticmethod】
本类没有实例状态(不存任何 self.xxx),所有方法都是静态的,
相当于"函数的命名空间"。调用方式:
    SessionManager.save_api_key("xxxx", True)
而不需要先 new 一个对象。这种写法适合纯工具类。

【关键概念:keyring 后端】
keyring 是跨平台库,实际存储位置依赖运行环境:
    Windows -> Windows Credential Manager(凭据管理器)
    macOS   -> Keychain
    Linux   -> Secret Service / D-Bus
在企业内网/打包环境中有时会出现后端不可用,所以代码里用 try/except 兜底。
"""
import keyring      # 第三方库:跨平台访问系统凭据存储
import json         # 标准库:读写 JSON
import os           # 标准库:文件路径操作
from typing import Optional   # 类型注解工具
# 复用 paths 模块,不再自己算路径
from utils.paths import get_data_path, ensure_data_dir


class SessionManager:
    """Session 管理器"""

    # 所有类常量用大写 + 下划线命名,这是 Python 的命名约定(PEP 8)。
    SERVICE_NAME = "glm_monitor"             # keyring 的"服务名",用于隔离不同应用的凭据
    APIKEY_KEYRING_KEY = "apikey"            # keyring 里存 API Key 用的固定"键"
    SESSION_FILE = "session.json"            # 会话信息文件名(相对 data 目录)

    @staticmethod
    def _get_session_file_path() -> str:
        """获取 session.json 的绝对路径(委托给 utils.paths)"""
        return get_data_path(SessionManager.SESSION_FILE)

    @staticmethod
    def save_api_key(api_key: str, remember: bool = False) -> bool:
        """
        保存 API Key

        Args:
            api_key: 智谱开放平台的 API Key
            remember: 是否"记住 API Key"(True 才把 Key 存进 keyring,下次免输入)

        Returns:
            是否保存成功

        【流程】
            1. remember_api_key 标志 -> 写入 data/session.json(明文,只是个开关)
            2. 如果用户勾了"记住 API Key" -> Key 写进 keyring(密文)
            3. 没勾的话,Key 只在内存里,关掉程序就丢
        """
        try:
            ensure_data_dir()

            # 把要保存的"非敏感信息"组装成字典(只是一个开关,Key 不进这里)
            session_data = {"remember_api_key": remember}

            # 'w' 模式:写(覆盖已有内容);encoding='utf-8' 保证中文不出乱码
            # with open(...) as f:上下文管理器,自动在结束时关闭文件,
            # 即使中间出异常也能正确释放,推荐始终这样写。
            session_file = SessionManager._get_session_file_path()
            with open(session_file, 'w', encoding='utf-8') as f:
                # ensure_ascii=False:中文直接写,不转成 \uXXXX 转义
                # indent=2:缩进 2 空格,文件更易读
                json.dump(session_data, f, ensure_ascii=False, indent=2)

            # 记住 API Key 时,把 Key 单独存进 keyring
            if remember:
                # set_password(服务名, 键, 密钥)
                # 服务名 + 键 组成唯一键,后续用同样参数取回
                keyring.set_password(
                    SessionManager.SERVICE_NAME,
                    SessionManager.APIKEY_KEYRING_KEY,
                    api_key
                )
            else:
                # 没勾"记住"就把可能残留的 keyring 条目清掉,避免旧 Key 泄漏
                SessionManager._try_delete_keyring(SessionManager.APIKEY_KEYRING_KEY)

            return True
        except Exception as e:
            # 出错只记录不抛出:不应因"保存失败"而中断主流程
            import logging
            logging.getLogger(__name__).warning("保存 API Key 失败: %s", e, exc_info=True)
            return False

    @staticmethod
    def load_api_key() -> Optional[str]:
        """
        加载已保存的 API Key

        Returns:
            API Key 字符串,或 None(没保存过 / 没勾记住 / 读取失败)
        """
        try:
            session_file = SessionManager._get_session_file_path()
            # 先检查文件存不存在,不存在直接返回 None
            if not os.path.exists(session_file):
                return None

            # 'r' 模式:读
            with open(session_file, 'r', encoding='utf-8') as f:
                # json.load 把 JSON 文件内容解析回 Python 字典
                session_data = json.load(f)

            # 顺手清理一次旧版本(账号密码时代)遗留的 keyring 条目(只生效一次)
            SessionManager._migrate_legacy_session(session_data, session_file)

            # 只有勾了"记住 API Key"才去 keyring 取
            if not session_data.get("remember_api_key"):
                return None

            return keyring.get_password(
                SessionManager.SERVICE_NAME,
                SessionManager.APIKEY_KEYRING_KEY
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("加载 API Key 失败: %s", e, exc_info=True)
            return None

    @staticmethod
    def clear_api_key() -> bool:
        """
        清除已保存的 API Key(切换 Key / 退出时调用)

        【流程】
            1. 删除 keyring 里的 API Key
            2. 删除 session.json 文件本身

        Returns:
            是否清除成功
        """
        try:
            # 先删 keyring 里的 Key
            SessionManager._try_delete_keyring(SessionManager.APIKEY_KEYRING_KEY)

            # 删 JSON 文件
            session_file = SessionManager._get_session_file_path()
            if os.path.exists(session_file):
                os.remove(session_file)

            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("清除 API Key 失败: %s", e, exc_info=True)
            return False

    @staticmethod
    def _try_delete_keyring(key: str) -> None:
        """
        best-effort 删除 keyring 条目(没有就忽略)

        【为什么单独抽出来】
            keyring 里可能本来就没有对应条目(用户没勾"记住"),
            此时 delete_password 会抛 PasswordDeleteError,属于预期情况,
            统一在这里 try/except 吞掉,并 debug 级别记录方便排查。
        """
        try:
            keyring.delete_password(SessionManager.SERVICE_NAME, key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(
                "keyring 删除失败(可能本来就没有): %s", e
            )

    @staticmethod
    def _migrate_legacy_session(session_data: dict, session_file: str) -> None:
        """
        清理旧版本(账号密码时代)遗留的会话数据。

        老的 session.json 形如 {"username": "...", "remember_me": true},
        对应的 keyring 里还存着一条以"用户名"为键的旧密码。
        检测到就把这条旧密码 best-effort 删掉,并把 session.json 覆盖成新格式,
        避免残留。失败也无所谓,不影响新流程;改完一次后文件就不再带 username,
        后续加载不会再进这里。
        """
        try:
            old_username = session_data.get("username")
            if not old_username:
                return
            # 删旧的账号密码遗留
            SessionManager._try_delete_keyring(old_username)
            # 覆盖成新格式(默认不记住,要求用户重新输入 API Key)
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump({"remember_api_key": False}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug("清理旧会话遗留失败(可忽略): %s", e)
