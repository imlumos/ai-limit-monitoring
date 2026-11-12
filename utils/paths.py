"""
路径工具 - 统一处理开发环境与打包环境的路径推算
================================================

【为什么单独抽这个模块】
原本 SessionManager / WindowManager / FloatWindow 三处各自写一遍
"开发环境用 __file__ 推算 / 打包环境用 sys.executable 或 sys._MEIPASS",
改起来容易漏。集中到一个地方,改一处生效三处。

【两个核心概念】
1. 项目根目录(PROJECT_ROOT):
   - 开发环境:<项目根> = utils/ 的上一级(本文件在 utils/ 下)
   - 打包环境:exe 所在目录
   用途:写"运行时数据"文件(data/session.json、data/window_state.json、data/app.log)

2. 资源根目录(RESOURCE_ROOT):
   - 开发环境:同 PROJECT_ROOT
   - 打包环境:PyInstaller 临时解压目录 sys._MEIPASS
   用途:读"打包内置资源"(config/settings.toml、faviconV2.png)
   注意:这个目录是只读的,不能往里写文件。

【历史 bug 修复(2026-06-24)】
原 session_manager.py 用 `os.path.dirname(os.path.dirname(current_dir))`
想推算项目根,但 utils/ 上两级是 `D:\\workplace`(项目 python 的父目录),
而真正项目根是 `D:\\workplace\\python`(utils/ 的上一级)。
session.json 是可选的所以 bug 一直没暴露,settings.toml 是必读的,
迁移到 paths.py 后立即崩溃。修复:只上一级。
"""
import os
import sys


def get_project_root() -> str:
    """获取项目根目录(可写)

    【开发环境】
        utils/paths.py 在 <项目根>/utils/ 下,所以 __file__ 上**一级**就是项目根。
        例:D:\\workplace\\python\\utils\\paths.py
            → dirname(__file__) = D:\\workplace\\python\\utils
            → 再上一次 = D:\\workplace\\python   ← 项目根

    【打包环境(frozen)】
        PyInstaller 把代码打包进 exe,运行时 __file__ 指向临时解压目录,
        那里是只读的。我们改用 sys.executable(exe 自身路径)反推:
            D:\\release\\AI额度悬浮窗.exe
            → dirname 一次 = D:\\release   ← exe 同级目录
        实际项目把数据存到 exe 同级目录(配合打包布局),如果需要存到上级再调层数。
    """
    if getattr(sys, 'frozen', False):
        # 打包后:数据存到 exe 同级目录
        return os.path.dirname(os.path.abspath(sys.executable))
    # 开发环境:本文件在 <项目根>/utils/ 下,上一级 = 项目根
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(current_dir)


def get_resource_root() -> str:
    """获取资源根目录(只读)

    【打包环境】
        PyInstaller 打包后,资源被解压到 sys._MEIPASS,
        退出时自动清理,所以这里只读不写。

    【开发环境】
        同项目根目录。
    """
    # hasattr 而不是 getattr(frozen),因为只有打包后才会有 _MEIPASS 属性
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return get_project_root()


def get_resource_path(relative_path: str) -> str:
    """获取打包资源的绝对路径(只读)

    Args:
        relative_path: 相对项目根目录的路径,例如 "config/settings.toml"

    Returns:
        绝对路径

    【典型用途】
        读 settings.toml、加载 faviconV2.png 等打包内置资源。
    """
    return os.path.join(get_resource_root(), relative_path)


def get_data_path(filename: str) -> str:
    """获取运行时数据文件的绝对路径(可写)

    Args:
        filename: 数据文件名,例如 "session.json"

    Returns:
        <项目根>/data/<filename>

    【典型用途】
        写 data/session.json、data/window_state.json 等运行时产生的文件。
    """
    return os.path.join(get_project_root(), 'data', filename)


def ensure_data_dir() -> None:
    """确保 data 目录存在,不存在则创建"""
    data_dir = os.path.join(get_project_root(), 'data')
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
