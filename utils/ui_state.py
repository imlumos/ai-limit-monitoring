"""
UI 状态存储 (ui_state)
======================

【这个文件做什么】
持久化"显示模式 + 账号选择"这类纯 UI 偏好到 data/ui_state.json。

【存什么】
  display_mode:        "single" | "multi"(单源聚焦 / 多源同屏)
  active_account_id:   单源模式下当前选中的账号 id
  visible_account_ids: 多源模式下勾选显示的账号 id 列表(空 = 显示全部启用账号)
  skip_update_version: 用户勾选"此版本不再提醒"的发行版 tag(如 "v1.0.1"),
                       更新检查时静默跳过;装好新版本后自然失效
  window_width:        用户拖拽出的窗口宽度(None = 用配置默认宽度)
  panel_height:        用户拖拽出的展开面板高度(None = 随内容自动长高)

【和 accounts.json 的区别】
accounts.json 是账号本身(凭据/平台),是"有什么";ui_state.json 是"怎么看"
(选哪个/选哪些)。分开存,删账号时这里残留的 id 由 UI 层自动忽略。
"""
import json
import logging
import os

from utils.paths import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)

STATE_FILE = "ui_state.json"

MODE_SINGLE = "single"
MODE_MULTI = "multi"


def _default() -> dict:
    return {
        "display_mode": MODE_SINGLE,
        "active_account_id": None,
        "visible_account_ids": [],
        "skip_update_version": None,
        "window_width": None,
        "panel_height": None,
    }


def load_ui_state() -> dict:
    """读取 UI 状态;文件缺失/损坏返回默认值(单源模式、无选择)。"""
    state = _default()
    path = get_data_path(STATE_FILE)
    if not os.path.exists(path):
        return state
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k in state:
            if k in raw:
                state[k] = raw[k]
    except Exception as e:
        logger.warning("读取 ui_state.json 失败,用默认值: %s", e)
    return state


def save_ui_state(state: dict) -> None:
    """写入 UI 状态(失败只记日志,不影响主流程)。"""
    ensure_data_dir()
    path = get_data_path(STATE_FILE)
    payload = _default()
    payload.update({k: state.get(k) for k in payload})
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存 ui_state.json 失败: %s", e)
