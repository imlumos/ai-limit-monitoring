"""
AI 额度悬浮窗 - 主入口
========================

【这个文件做什么】
程序从这里启动,负责:
  1. 创建一个 Qt 应用程序对象(QApplication)
  2. 创建主窗口(FloatWindow)的实例
  3. 把程序交给 Qt 的事件循环,持续运行直到用户关闭

【关键概念:Python 入口模式】
Python 没有像 C/Java 那样的 main 函数强制入口。
习惯写法是用 `if __name__ == "__main__":`(见文件末尾)。
直接 `python main.py` 运行时,该条件为 True,自动调用 main()。
被别的文件 `import main` 时,该条件为 False,不会自动运行——
这就是这种写法的好处:既可独立运行,也可作为模块被导入。

【日志初始化】
所有日志通过 utils.log_setup 配置后,业务模块用 logging.getLogger(__name__)
就能拿到统一的 logger。这里在创建 QApplication 之前先初始化日志,
保证启动过程中任何错误都能被记录。
"""
# sys 是 Python 标准库,这里用 sys.argv(命令行参数列表)和 sys.exit(退出程序)
import sys
# QApplication 是 Qt 程序的"管家",任何 PySide6 GUI 程序有且只能有一个 QApplication 实例
# 它负责:事件循环、窗口管理、字体、主题、国际化等全局事务
from PySide6.QtWidgets import QApplication, QMessageBox
# 从子包 ui 中导入 FloatWindow 类。
# 注意 ui 是一个"包"(package),因为它里面有 __init__.py,Python 才把它识别为包
from ui.float_window import FloatWindow
# 日志初始化(必须在第一次使用 logging 之前调用)
from utils.log_setup import setup_logging
from utils.account_store import AccountStoreError
from utils.proxy_config import initialize_proxy_config, ProxyConfigError


def main():
    """主函数:程序业务流程的入口"""
    # 0) 配置日志(必须在创建窗口之前,保证启动过程的错误也能被记录)
    setup_logging()

    # 1) 创建 QApplication,传入命令行参数。
    #    即使我们不用这些参数,Qt 也会读取其中一些(比如 -style 修改主题)。
    app = QApplication(sys.argv)

    # 在任何账号读取/网络采集之前加载本机代理配置。
    try:
        initialize_proxy_config()
    except ProxyConfigError as e:
        QMessageBox.critical(None, "代理配置不可用", str(e))
        return

    # 2) 创建悬浮窗对象。FloatWindow.__init__ 里会完成所有 UI 搭建、定时器、托盘等初始化。
    #    此时窗口还不可见(GUI 程序里,窗口默认是隐藏的,需要调用 show())。
    try:
        window = FloatWindow()
    except AccountStoreError as e:
        QMessageBox.critical(None, "账号存储不可用", str(e))
        return  # 存储异常时不覆盖账号文件,也不开始轮询。

    # 3) 执行启动流程:启动多账号轮询;有账号收起显示,无账号展开账号管理
    window.startup_flow()

    # 4) 显示窗口。
    #    统一由 main.py 控制 show(),startup_flow 内部不再 show,
    #    避免"重复 show()"和"职责分散"。
    window.show()

    # 5) 进入 Qt 的"事件循环"(Event Loop)。
    #    app.exec() 会阻塞在这里,持续处理鼠标、键盘、定时器、网络回调等事件,
    #    直到窗口被关闭或调用 app.quit()。
    #    sys.exit(...) 的作用:等事件循环结束后,把返回值作为进程退出码传给操作系统,
    #    并刷新缓冲区。返回 0 表示正常退出。
    sys.exit(app.exec())


# Python 程序的经典入口判断:
# __name__ 是 Python 给每个模块内置的变量。
#   - 直接运行该文件时,__name__ == "__main__"
#   - 被其他文件 import 时,__name__ == "main"(模块名)
# 这种写法保证:只有直接运行才执行 main(),被导入时不执行。
if __name__ == "__main__":
    main()
