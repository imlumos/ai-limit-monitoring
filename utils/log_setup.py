"""
日志初始化 - 统一配置全项目的 logging
======================================

【这个文件做什么】
在程序启动时(main.py 里)调用一次 setup_logging(),之后任何模块用
    import logging
    logger = logging.getLogger(__name__)
就能拿到统一配置好的 logger。

【为什么不再用 print】
print 有几个硬伤:
    - 没有等级(DEBUG/INFO/WARNING/ERROR)
    - 打包后(console=False)用户看不到
    - 不能写到文件、不能按大小切分
    - 没有 timestamp、模块名、行号等上下文

logging 都能解决,而且是 Python 标准库,无需引入第三方依赖。

【输出位置】
    - 控制台(开发时方便看)
    - data/app.log 文件(用户运行时排查问题)
    每次启动覆盖旧日志(用 'w' 模式),保持文件不会无限增长。
    如果需要长期保留,改成 RotatingFileHandler 即可。

【为什么不用 dictConfig / YAML】
项目小,直接 basicConfig 几行就够。等需求复杂了再升级。
"""
import logging
import sys
from utils.paths import get_data_path, ensure_data_dir
from utils.redaction import RedactingFormatter


# 日志格式:时间 - 等级 - 模块名 - 消息
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 日志文件名(放在 data/ 下)
LOG_FILENAME = "app.log"


def setup_logging(level: int = logging.INFO) -> None:
    """
    配置全局 logging

    Args:
        level: 最低输出等级(默认 INFO,DEBUG 等级会更详细但更啰嗦)

    【典型用法】
        from utils.log_setup import setup_logging
        setup_logging()   # 在 main.py 第一行调用

        # 任何其他模块
        import logging
        logger = logging.getLogger(__name__)
        logger.info("启动成功")
        logger.warning("token 即将过期")
        logger.exception("网络请求失败")   # 自动带堆栈

    【为什么不返回 logger】
        basicConfig 配置的是 root logger,所有模块的 getLogger() 都会
        沿着继承链向上找到它,所以配置一次全局生效。
    """
    # 准备日志文件路径(在 data/ 目录下)
    ensure_data_dir()
    log_path = get_data_path(LOG_FILENAME)

    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(log_path, mode='w', encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(RedactingFormatter(LOG_FORMAT, DATE_FORMAT))
    # 配置 root logger;格式化器覆盖文件和控制台的异常输出。
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        handlers=handlers,
    )

    # 给第三方库(如 urllib3)降噪,避免日志太吵
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("keyring").setLevel(logging.WARNING)
