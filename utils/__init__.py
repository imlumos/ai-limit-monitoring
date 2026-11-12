# utils package
# ============================================================
# 【这个文件做什么】
# 这是 utils 包的"标识文件"。在 Python 中,一个目录要被识别为"包"(package),
# 必须包含一个 __init__.py 文件(可以是空文件)。
#
# 【为什么需要包】
# 有了这个文件,utils 目录就成为一个包,我们才能这样导入:
#     from utils.session_manager import SessionManager
#     from utils import autostart
#
# 【Python 3.3+ 的细节】
#   - Python 3.3+ 引入了"命名空间包"(namespace package),即使没有 __init__.py
#     也能 import,但显式写上 __init__.py 仍是主流做法,这样目录结构更清晰、
#     兼容性更好(打包工具、IDE 识别更稳定)。
#   - __init__.py 里也可以写代码,会在"包第一次被导入时"自动执行一次,
#     常用于做一些包级别的初始化。这里不需要,保持空注释即可。
# ============================================================
