"""本机代理配置。首次启动创建 data/proxy.toml,修改后重启生效。"""
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from utils.paths import get_data_path


TEMPLATE = '''# 修改后重启软件生效。无需设置终端环境变量。
# mode: environment = 沿用环境/系统代理; direct = 直连; custom = 使用 url。
# 支持 HTTP/HTTPS 代理,不支持 SOCKS 和 URL 内的用户名/密码。
[proxy]
mode = "environment"
url = ""

# 全部平台使用本地代理时,把上面的配置改为:
# mode = "custom"
# url = "http://127.0.0.1:7890"  # 替换为实际 HTTP 或混合代理端口

# 第一版全部 AI 平台共用上述设置,不支持按平台单独配置。
'''


class ProxyConfigError(ValueError):
    """不含配置原文或代理凭据的可展示错误。"""


@dataclass(frozen=True)
class ProxySettings:
    mode: str = "environment"
    url: str = field(default="", repr=False)


_config = ProxySettings()


def _settings(raw) -> ProxySettings:
    if not isinstance(raw, dict) or set(raw) - {"mode", "url"}:
        raise ProxyConfigError("代理配置仅支持 mode 和 url 字段，请检查 data/proxy.toml")
    mode = raw.get("mode", "environment")
    url = raw.get("url", "")
    if not isinstance(mode, str) or mode not in {"environment", "direct", "custom"}:
        raise ProxyConfigError("代理 mode 必须为 environment、direct 或 custom")
    if not isinstance(url, str):
        raise ProxyConfigError("代理 url 必须是字符串")
    url = url.strip()
    if mode != "custom":
        if url:
            raise ProxyConfigError("填写代理 url 时需将 mode 设为 custom；其他模式请清空 url")
        return ProxySettings(mode)
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and not any(c.isspace() or ord(c) < 32 for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise ProxyConfigError("代理地址无效：请填写 http://主机:端口 或 https://主机:端口，不含账号密码或路径")
    return ProxySettings(mode, url)


def load_proxy_config(path: Path) -> ProxySettings:
    """读写只涉及指定配置文件;无网络访问,不回显解析失败的原文。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as f:
                f.write(TEMPLATE)
        except FileExistsError:
            pass
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ProxyConfigError("无法读取代理配置，请检查 data/proxy.toml 的 TOML 格式和文件权限；未覆盖已有文件") from None
    if set(raw) - {"proxy"}:
        raise ProxyConfigError("第一版代理配置仅支持统一的 [proxy] 配置段")
    return _settings(raw.get("proxy", {}))


def initialize_proxy_config() -> None:
    global _config
    # 全部验证通过后才替换配置;错误时启动流程停止,不静默回退直连。
    _config = load_proxy_config(Path(get_data_path("proxy.toml")))


def get_proxy_settings() -> ProxySettings:
    return _config


def parse_proxy_section(raw) -> ProxySettings:
    """校验 [proxy] 段(设置界面/导入用,与读文件同一套规则)。"""
    return _settings(raw)


_SAVED_HEADER = ("# 由设置界面自动保存,也可手动修改(手动修改后重启软件生效)。\n"
                 "# mode: environment = 沿用环境/系统代理; direct = 直连; custom = 使用 url。\n"
                 "# 支持 HTTP/HTTPS 代理,不支持 SOCKS 和 URL 内的用户名/密码。\n")


def save_and_apply_proxy(settings: ProxySettings) -> None:
    """写 data/proxy.toml 并立即生效(下一次请求新建的会话即使用新代理)。"""
    global _config
    import json
    import os
    settings = _settings({"mode": settings.mode, "url": settings.url})   # 再校验一次
    path = Path(get_data_path("proxy.toml"))
    tmp = path.with_name(path.name + ".tmp")
    text = (_SAVED_HEADER + "[proxy]\n"
            f"mode = {json.dumps(settings.mode)}\nurl = {json.dumps(settings.url)}\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ProxyConfigError("代理配置保存失败，请检查 data 目录权限") from None
    _config = settings
