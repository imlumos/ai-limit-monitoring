"""
用户可调配置 (app_settings)
===========================

【这个文件做什么】
设置对话框里能改的配置:软件标识、刷新间隔、更新策略、外观(代理单独在 proxy_config)。
- 外观背景图由用户选择后复制到 data/background.<扩展名>,配置里只存文件名。
- 内置默认值在 config/settings.toml(打包进程序,只读);
- 用户改过的值写到 data/settings.toml,启动时覆盖在默认值之上;
- 导入/导出把"可调配置 + 代理 + 图标/背景图片"写入一个 TOML 文件,不含任何账号或凭据。

校验集中在 validate_settings:界面、启动加载、导入都走它,非法值整体拒绝,
不会半写入。
"""
import copy
import base64
import binascii
import json
import logging
import os
import re
import shutil
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Tuple

from utils.paths import get_data_path, ensure_data_dir
from utils.proxy_config import ProxySettings, ProxyConfigError, parse_proxy_section

logger = logging.getLogger(__name__)

USER_FILE = "settings.toml"
MIN_INTERVAL, MAX_INTERVAL = 1, 120     # 刷新间隔范围(分钟)
DEFAULT_APP_NAME = "AI额度悬浮窗"
APP_ICON_FILE = "app-icon.ico"
APP_ICON_EXTS = (".ico", ".png", ".jpg", ".jpeg", ".bmp", ".webp")

# 每段允许的键及类型(只有这些能被用户覆盖/导入)
_SCHEMA = {
    "application": {"name": str, "icon": str},
    "refresh": {"interval_minutes": int},
    "update": {"enabled": bool, "check_on_startup": bool},
    "appearance": {"bg_color": str, "bg_opacity": int, "bg_image": str},
}

# 背景图只能是 data/ 下固定前缀的文件名(防止导入的配置指向任意路径)
BG_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
BG_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_BG_IMAGE_RE = re.compile(r"^background\.(png|jpg|jpeg|bmp|webp)$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class SettingsError(ValueError):
    """可直接展示给用户的配置错误(不含文件原文)。"""


def validate_settings(raw) -> dict:
    """校验可调配置片段,返回规范化后的 {段: {键: 值}};允许只给部分段/键。"""
    if not isinstance(raw, dict):
        raise SettingsError("配置格式无效")
    unknown = set(raw) - set(_SCHEMA)
    if unknown:
        raise SettingsError(f"不支持的配置段: {', '.join(sorted(unknown))}")
    out = {}
    for section, values in raw.items():
        if not isinstance(values, dict):
            raise SettingsError(f"[{section}] 必须是配置段")
        schema = _SCHEMA[section]
        bad = set(values) - set(schema)
        if bad:
            raise SettingsError(f"[{section}] 不支持的配置项: {', '.join(sorted(bad))}")
        for key, value in values.items():
            typ = schema[key]
            # bool 是 int 的子类,int 字段要排除 True/False
            if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
                raise SettingsError(f"[{section}] {key} 类型错误")
        out[section] = dict(values)
    interval = out.get("refresh", {}).get("interval_minutes")
    if interval is not None and not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise SettingsError(f"刷新间隔需在 {MIN_INTERVAL}~{MAX_INTERVAL} 分钟之间")
    look = out.get("appearance", {})
    identity = out.get("application", {})
    if "name" in identity:
        name = identity["name"].strip()
        if (not 1 <= len(name) <= 40 or name.endswith(".")
                or any(ord(c) < 32 or ord(c) == 127 or c in '<>:"/\\|?*' for c in name)
                or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", name.split(".")[0], re.IGNORECASE)):
            raise SettingsError("软件名称需为 1~40 个字符,不能包含换行或 Windows 文件名禁用字符")
        identity["name"] = name
    if identity.get("icon", "") not in ("", APP_ICON_FILE):
        raise SettingsError("软件图标文件名无效")
    if "bg_color" in look and not _COLOR_RE.match(look["bg_color"]):
        raise SettingsError("背景颜色需为 #RRGGBB 格式")
    if "bg_opacity" in look and not 0 <= look["bg_opacity"] <= 100:
        raise SettingsError("背景不透明度需在 0~100 之间")
    if look.get("bg_image") and not _BG_IMAGE_RE.match(look["bg_image"]):
        raise SettingsError("背景图文件名无效")
    return out


def merge(base: dict, override: dict) -> dict:
    """把校验过的覆盖值合并进配置(返回新字典,不改 base)。"""
    merged = copy.deepcopy(base)
    for section, values in override.items():
        merged.setdefault(section, {}).update(values)
    return merged


def user_settings_path() -> Path:
    return Path(get_data_path(USER_FILE))


def load_user_settings() -> dict:
    """读 data/settings.toml;不存在返回 {};损坏/非法只记日志并忽略(回退内置默认)。"""
    path = user_settings_path()
    if not path.exists():
        return {}
    try:
        return validate_settings(tomllib.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, SettingsError) as e:
        logger.warning("用户配置无效,已忽略并使用默认值(%s)", type(e).__name__)
        return {}


def current_overrides(config: dict) -> dict:
    """从完整配置里取出可调部分(只取 schema 内、且存在的键)。"""
    return {section: {k: config[section][k] for k in keys if k in config.get(section, {})}
            for section, keys in _SCHEMA.items() if section in config}


def dumps_toml(sections: dict, header: str = "") -> str:
    """把 {段: {键: 标量}} 写成 TOML(只需支持 bool/int/str;str 用 JSON 转义,与 TOML 基本字符串兼容)。"""
    lines = [header.rstrip()] if header else []
    for section, values in sections.items():
        lines.append(f"\n[{section}]")
        for key, value in values.items():
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, int):
                text = str(value)
            else:
                text = json.dumps(value, ensure_ascii=False)
            lines.append(f"{key} = {text}")
    return "\n".join(lines).lstrip("\n") + "\n"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SettingsError(f"保存失败,请检查 {path.name} 所在目录的权限") from None


def save_user_settings(overrides: dict) -> None:
    ensure_data_dir()
    _atomic_write(user_settings_path(),
                  dumps_toml(validate_settings(overrides),
                             "# 由设置界面自动保存,覆盖程序内置的 config/settings.toml"))


# ====== 背景图 ======
def bg_image_path(name: str) -> str:
    """配置里的背景图文件名 → data/ 下的绝对路径;未设置或名字非法返回 ""。"""
    return get_data_path(name) if name and _BG_IMAGE_RE.match(name) else ""


def store_background_image(src: str) -> str:
    """把用户选的图片复制到 data/background.<ext>(覆盖旧的),返回文件名。"""
    ext = Path(src).suffix.lower()
    if ext not in BG_IMAGE_EXTS:
        raise SettingsError("仅支持 PNG / JPG / BMP / WEBP 图片")
    try:
        if os.path.getsize(src) > BG_IMAGE_MAX_BYTES:
            raise SettingsError("图片不能超过 10 MB")
        ensure_data_dir()
        name = "background" + ext
        dst = Path(get_data_path(name))
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        raise SettingsError("复制图片失败,请检查文件和 data 目录权限") from None
    for other in BG_IMAGE_EXTS:            # 换了扩展名时清掉旧图
        if other != ext:
            try:
                Path(get_data_path("background" + other)).unlink()
            except OSError:
                pass
    return name


def remove_background_images() -> None:
    for ext in BG_IMAGE_EXTS:
        try:
            Path(get_data_path("background" + ext)).unlink()
        except OSError:
            pass


# ====== 软件名称 / 图标 ======
def app_icon_path(name: str) -> str:
    return get_data_path(name) if name == APP_ICON_FILE else ""


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SettingsError(f"保存 {path.name} 失败,请检查 data 目录权限") from None


def _read_asset_image(data: bytes):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImageReader
    if not data or len(data) > BG_IMAGE_MAX_BYTES:
        raise SettingsError("图片不能为空或超过 10 MB")
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    fmt = bytes(reader.format()).lower()
    if fmt not in (b"png", b"jpeg", b"bmp", b"webp", b"ico"):
        raise SettingsError("仅支持 ICO / PNG / JPG / BMP / WEBP 图片")
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > 4096:
        raise SettingsError("图片尺寸无效或超过 4096×4096")
    image = reader.read()
    if image.isNull():
        raise SettingsError("无法读取图片")
    return image, fmt


def read_app_icon(src: str) -> bytes:
    """读取图片并转为 Windows 快捷方式也能使用的 ICO。"""
    from PySide6.QtCore import Qt, QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage, QPainter
    if Path(src).suffix.lower() not in APP_ICON_EXTS:
        raise SettingsError("图标仅支持 ICO / PNG / JPG / BMP / WEBP")
    try:
        with open(src, "rb") as source:
            image, _ = _read_asset_image(source.read(BG_IMAGE_MAX_BYTES + 1))
    except OSError:
        raise SettingsError("无法读取图标文件") from None
    image = image.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    square = QImage(256, 256, QImage.Format.Format_ARGB32)
    square.fill(Qt.GlobalColor.transparent)
    painter = QPainter(square)
    painter.drawImage((256 - image.width()) // 2, (256 - image.height()) // 2, image)
    painter.end()
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not square.save(buffer, "ICO"):
        raise SettingsError("无法转换图标")
    return bytes(encoded)


def store_app_icon(src: str) -> str:
    """复制并转换到 data/app-icon.ico。"""
    _atomic_write_bytes(Path(get_data_path(APP_ICON_FILE)), read_app_icon(src))
    return APP_ICON_FILE


# ====== 导入 / 导出 ======
_EXPORT_HEADER = ("# AI 额度悬浮窗 配置导出\n"
                  "# 含软件名称、图标、刷新、更新、外观和代理;图片以 Base64 内嵌,不含账号或 API Key")


def _asset_names(overrides: dict) -> dict:
    return {key: name for key, name in {
        "app_icon": overrides.get("application", {}).get("icon", ""),
        "background_image": overrides.get("appearance", {}).get("bg_image", ""),
    }.items() if name}


def _validate_asset(name: str, data: bytes) -> None:
    _, fmt = _read_asset_image(data)
    if name == APP_ICON_FILE:
        if fmt != b"ico":
            raise SettingsError("软件图标必须为 ICO 图片")
    elif not _BG_IMAGE_RE.fullmatch(name):
        raise SettingsError("图片文件名无效")


def export_settings(path: str, overrides: dict, proxy: ProxySettings) -> None:
    sections = dict(validate_settings(overrides))
    sections["proxy"] = {"mode": proxy.mode, "url": proxy.url}
    assets = {}
    for key, name in _asset_names(sections).items():
        try:
            with open(get_data_path(name), "rb") as source:
                data = source.read(BG_IMAGE_MAX_BYTES + 1)
        except OSError:
            raise SettingsError(f"无法导出 {name},请重新选择图片或清除该设置") from None
        _validate_asset(name, data)
        assets[key] = base64.b64encode(data).decode("ascii")
    if assets:
        sections["assets"] = assets
    _atomic_write(Path(path), dumps_toml(sections, _EXPORT_HEADER))


def import_settings(path: str, *, assets: dict | None = None) -> Tuple[dict, ProxySettings]:
    """读导出文件并整体校验;任一项非法则抛 SettingsError,调用方什么都不改。"""
    try:
        with open(path, "rb") as source:
            content = source.read(30 * 1024 * 1024 + 1)
        if len(content) > 30 * 1024 * 1024:
            raise SettingsError("配置文件不能超过 30 MB")
        raw = tomllib.loads(content.decode("utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise SettingsError("无法读取文件,请确认是本程序导出的 TOML 配置") from None
    proxy_raw = raw.pop("proxy", None)
    asset_raw = raw.pop("assets", {})
    overrides = validate_settings(raw)
    try:
        proxy = parse_proxy_section(proxy_raw) if proxy_raw is not None else None
    except ProxyConfigError as e:
        raise SettingsError(str(e)) from None
    if not isinstance(asset_raw, dict):
        raise SettingsError("图片资源配置无效")
    names = _asset_names(overrides)
    decoded = {}
    for key, encoded in asset_raw.items():
        if key not in names or not isinstance(encoded, str):
            raise SettingsError("图片资源与配置不匹配")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise SettingsError("图片资源编码无效") from None
        _validate_asset(names[key], data)
        decoded[names[key]] = data
    if assets is not None:
        assets.update(decoded)
    return overrides, proxy


@contextmanager
def installed_assets(assets: dict):
    """安装已校验的导入图片;保存设置失败时恢复原图片。"""
    previous = {}
    try:
        for name, data in assets.items():
            _validate_asset(name, data)
            path = Path(get_data_path(name))
            previous[name] = path.read_bytes() if path.exists() else None
            _atomic_write_bytes(path, data)
        yield
    except Exception as error:
        for name, data in previous.items():
            path = Path(get_data_path(name))
            if data is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, data)
        if isinstance(error, OSError):
            raise SettingsError("无法保存导入图片,请检查 data 目录权限") from None
        raise
