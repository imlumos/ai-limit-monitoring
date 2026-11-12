"""
开机自启动管理(Windows 注册表方式)
====================================

【这个文件做什么】
让程序"开机自动启动"或取消开机启动。原理:读写 Windows 注册表里的
"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" 键,
Windows 开机登录时会自动执行这里登记的所有程序。

【为什么用 HKCU(HKEY_CURRENT_USER)?】
注册表有 HKLM(本机所有用户)和 HKCU(当前用户)两大根。
- 写 HKLM 需要管理员权限,会弹 UAC,体验差。
- 写 HKCU 不需要管理员,只对当前用户生效——对个人工具完全够用。

【winreg 是什么】
Python 标准库的 Windows 专用模块,封装了 Windows API 来操作注册表。
非 Windows 平台没有这个模块,所以本文件本身有平台限制,
在 Linux/macOS 上 import winreg 会直接失败。

【关键概念:with 语句 + 注册表句柄】
winreg.OpenKey 返回一个"句柄"(handle,操作系统资源的引用),
用完必须关闭,否则会泄漏。with 语句保证无论是否异常都自动关闭,
和 with open(...) 的用法一致。
"""
import sys
import logging
import winreg   # Python 标准库,Windows 注册表访问(仅 Windows 可用)

# 模块级 logger(统一走 logging,与全项目约定一致,不再用 print)
logger = logging.getLogger(__name__)

APP_NAME = "AI额度悬浮窗"   # 注册表里的"值名称",作为我们这个程序的唯一标识
# 旧版本(项目原名"GLM额度悬浮窗")写入 Run 键的值名。改名后必须检测并清理,
# 否则旧条目会一直指向旧 exe 路径:轻则残留死条目,重则开机新旧 exe 双开。
OLD_APP_NAME = "GLM额度悬浮窗"
# r"..." 是"原始字符串",反斜杠不被当转义符。
# 注册表路径里的反斜杠很多,加 r 前缀就不用写 \\\\ 了。
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _cleanup_legacy_value() -> None:
    """删除旧版自启注册表条目(值名 OLD_APP_NAME)。幂等:不存在时静默返回。

    【为什么要清理】
        改名前用户若已开启自启,注册表里留着一条 "GLM额度悬浮窗" = 旧 exe 路径。
        不清理的话,旧 exe 还在的机器开机时会双开;旧 exe 已删则残留死条目。
        挂在 set_enabled() 的开关路径上调用,用户下次操作自启开关时自动完成迁移。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, OLD_APP_NAME)
        logger.info("已清理旧版自启注册表条目 %r", OLD_APP_NAME)
    except FileNotFoundError:
        # 干净机器(或已清理过),无事可做
        pass
    except OSError as e:
        # 清理失败不影响主流程,只记日志:残留条目无害于新版本运行
        logger.warning("清理旧版自启条目失败: %s", e, exc_info=True)


def is_enabled() -> bool:
    """查询开机自启是否已开启

    【思路】
        试着去注册表里读 APP_NAME 这个值,
        - 读到 -> 已开启
        - 抛 FileNotFoundError -> 没这个值 -> 未开启
        - 其他 OSError -> 注册表访问异常,保守起见也返回未开启

    【过渡期兼容(项目改名)】
        存量用户的注册表里还是旧值名 OLD_APP_NAME("GLM额度悬浮窗"),
        且尚未触发过清理。若只查新值名,UI 会误报"未开启",用户重新勾选
        才会走清理迁移。所以这里双查:任一值名存在都算已开启。
        两次都是纯读操作,零成本。
    """
    try:
        # OpenKey(根键, 子路径, 保留参数0, 权限)
        # KEY_QUERY_VALUE 表示"我只要读值",权限最小化原则
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_QUERY_VALUE) as k:
            # QueryValueEx(句柄, 值名) 返回 (值, 类型)
            # 我们不关心具体值,只要不抛异常就说明条目存在
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        pass   # 新值名不存在,继续查旧值名(见下方过渡期兼容说明)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_QUERY_VALUE) as k:
            winreg.QueryValueEx(k, OLD_APP_NAME)
            return True
    except OSError:
        # FileNotFoundError(没登记)也是 OSError 的子类,一条 except 统一覆盖;
        # 任何注册表访问异常都保守地视为"未开启"
        return False


def set_enabled(enable: bool) -> bool:
    """开启或关闭开机自启

    Args:
        enable: True=开启, False=关闭

    Returns:
        是否操作成功

    【开发环境直接返回失败】
        非 PyInstaller 打包环境(直接 python 运行)下,sys.executable 是
        python.exe,写进注册表会让开机时直接跑一个空的解释器(没有脚本
        参数),既无意义又有副作用。所以开发环境一律不写注册表,返回 False
        让 UI 把复选框回退;真正生效的是打包后的 exe 场景。
    """
    # 开发环境(未打包):不写注册表,避免开机自动跑 python.exe
    if not getattr(sys, 'frozen', False):
        return False

    if enable:
        # sys.executable:打包后是 exe 完整路径,例如 D:\\release\\AI额度悬浮窗.exe
        exe_path = sys.executable
        # 先清掉旧值名条目,再写新值名:保证注册表里始终只有一条自启记录
        _cleanup_legacy_value()
        try:
            # KEY_SET_VALUE 表示"我要写值"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as k:
                # SetValueEx(句柄, 值名, 保留0, 类型, 数据)
                # REG_SZ = 字符串类型
                # 数据外面套双引号:防止路径含空格时被 Windows 当成多个参数
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
                return True
        except OSError as e:
            logger.warning("设置开机自启失败: %s", e, exc_info=True)
            return False
    else:
        # 关闭:删除注册表里的这个值
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, APP_NAME)
        except FileNotFoundError:
            # 本来就不存在,删不删都一样,视为成功
            pass
        except OSError as e:
            logger.warning("取消开机自启失败: %s", e, exc_info=True)
            return False
        # 新旧值名一并清理:存量用户可能只有旧条目,只删新的会"关不掉"
        _cleanup_legacy_value()
        return True
