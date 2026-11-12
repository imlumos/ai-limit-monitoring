"""
UI 颜色工具 - 颜色定义和分级
============================

【这个文件做什么】
集中管理项目里用到的颜色,尤其是"按百分比分级"的逻辑。

【为什么单独抽这个模块】
原本 float_window._get_percentage_color 和 detail_panel._draw_quota_item
里都各自写了 "<30 绿 / <70 黄 / >=70 红" 的判断,
改阈值要找两处。提为公共函数,改一处生效两处,语义也更清晰。

【关键概念:QColor】
Qt 的颜色对象,支持 RGBA(RGB + 透明度 alpha)。
本项目用得最多的几种:
    QColor(76, 175, 80)     绿色(健康)
    QColor(255, 193, 7)     黄色(注意)
    QColor(244, 67, 54)     红色(危险)
    QColor(0, 0, 0, 178)    半透明黑色背景(178/255 ≈ 0.7)
"""
from PySide6.QtGui import QColor


# ====== 百分比阈值常量(可调) ======
PERCENTAGE_GREEN_THRESHOLD = 30    # <30 显示绿色
PERCENTAGE_YELLOW_THRESHOLD = 70   # 30~70 显示黄色,>=70 显示红色

# ====== 颜色定义(集中管理,改一处生效全局) ======
COLOR_GREEN = QColor(76, 175, 80)      # 健康
COLOR_YELLOW = QColor(255, 193, 7)     # 注意
COLOR_RED = QColor(244, 67, 54)        # 危险

# UI 通用色
COLOR_TEXT_WHITE = QColor(255, 255, 255)
COLOR_TEXT_GRAY = QColor(150, 150, 150)
COLOR_TEXT_SECONDARY = QColor(200, 200, 200)
COLOR_PROGRESS_BAR_BG = QColor(60, 60, 60)


def get_percentage_color(percentage: int) -> QColor:
    """
    根据百分比返回颜色(数值越大越红)

    【设计意图】
        百分比代表"额度使用率",越高越接近上限越危险。
        <30% 绿色(健康),<70% 黄色(注意),>=70% 红色(危险)。
        颜色和进度条颜色逻辑一致,保持视觉统一。

    【调用方】
        - FloatWindow._draw_float_window_content:悬浮条圆点颜色
        - DetailPanel._draw_quota_item:进度条前景色
    """
    if percentage < PERCENTAGE_GREEN_THRESHOLD:
        return COLOR_GREEN
    elif percentage < PERCENTAGE_YELLOW_THRESHOLD:
        return COLOR_YELLOW
    else:
        return COLOR_RED
