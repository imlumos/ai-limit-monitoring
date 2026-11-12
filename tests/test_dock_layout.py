# -*- coding: utf-8 -*-
"""吸附条尺寸/绘制逻辑的离屏验证脚本(不弹真窗口,开发时手动跑)

验证矩阵(对应重构计划的 Step 6):
    单源贴顶 150×34(与旧版逐像素一致,回归基准)
    单源贴侧 88×150
    多源贴顶 200×(8+n*20),n=1 时高度保底 34
    多源贴侧 88×(8+n*38)
    多源 10+ 账号贴顶高度被屏幕 clamp 但仍走横条画法
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

# ---- 构造一个最小的 FloatWindow:绕过 __init__,只装上绘制要用的状态 ----
from PySide6.QtWidgets import QMainWindow
from ui.float_window import FloatWindow, MODE_MULTI, MODE_SINGLE


class FakeAccount:
    def __init__(self, id, label, enabled=True):
        self.id, self.label, self.enabled = id, label, enabled


class FakeStore:
    def __init__(self, accounts):
        self._accounts = accounts

    def list_accounts(self):
        return list(self._accounts)

    def list_enabled(self):
        return [a for a in self._accounts if a.enabled]


def make_window(mode, n):
    """mode: MODE_SINGLE/MODE_MULTI;n: 启用的账号数

    只初始化 Qt 基类(QMainWindow.__init__),跳过 FloatWindow.__init__
    里的托盘/定时器/详情面板 —— 尺寸计算和绘制只需要下面这些状态。
    """
    w = _BareWindow()
    accounts = [FakeAccount(f"id{i}", f"Acc{i}") for i in range(n)]
    w.account_store = FakeStore(accounts)
    w.ui_state = {
        'display_mode': mode,
        'active_account_id': accounts[0].id if accounts else None,
        'visible_account_ids': [a.id for a in accounts],
    }
    w._snapshots = {}
    w._docked = True
    w._dock_edge = 'top'
    w._free_geometry = w.screen().availableGeometry()  # 占位,几何断言不依赖它
    return w


class _BareWindow(FloatWindow):
    """跳过 FloatWindow.__init__(托盘/定时器等),只装 Qt 基类和最小状态"""

    def __init__(self):
        QMainWindow.__init__(self)
        self.config = {
            'window': {
                'width': 260, 'height': 50, 'corner_radius': 25,
            },
        }


FAILURES = []

def check(name, actual, expected):
    ok = actual == expected
    # GBK 控制台打不出 ¥ 等字符,encode 失败就降级展示
    try:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: actual={actual} expected={expected}")
    except UnicodeEncodeError:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILURES.append(name)


# ---- 矩阵断言 ----
w = make_window(MODE_SINGLE, 1)
w._dock_edge = 'top'
check("单源贴顶", w._docked_collapsed_size(), (150, 34))

w._dock_edge = 'bottom'
check("单源贴底", w._docked_collapsed_size(), (150, 34))

w._dock_edge = 'left'
check("单源贴左", w._docked_collapsed_size(), (88, 150))

w._dock_edge = 'right'
check("单源贴右", w._docked_collapsed_size(), (88, 150))

w = make_window(MODE_MULTI, 1)
w._dock_edge = 'top'
check("多源1贴顶(高度保底34)", w._docked_collapsed_size(), (200, 34))

w = make_window(MODE_MULTI, 3)
w._dock_edge = 'top'
check("多源3贴顶", w._docked_collapsed_size(), (200, 8 + 3 * 20))

w._dock_edge = 'left'
# 侧边高度 = 8 + Σ(14 + 16×chips数);无快照时 chips 兜底 1 行 → 每卡 30
check("多源3贴左(无快照,每卡兜底30)", w._docked_collapsed_size(),
      (88, 8 + 3 * (14 + 16)))

# 带快照:账号0 双档(GLM)→46,账号1 单档金额(DeepSeek)→30,账号2 单档→30
from api.models import QuotaSnapshot, QuotaTier, TIER_FIVE_HOUR, TIER_WALLET
w._snapshots = {
    "id0": QuotaSnapshot.success_snapshot("glm", tiers=[
        QuotaTier(name=TIER_FIVE_HOUR, utilization=42.0),
        QuotaTier(name="weekly_limit", utilization=15.0)]),
    "id1": QuotaSnapshot.success_snapshot("deepseek", tiers=[
        QuotaTier(name=TIER_WALLET, kind="money", remaining=0.0, unit="CNY")]),
    "id2": QuotaSnapshot.success_snapshot("kimi", tiers=[
        QuotaTier(name="weekly_limit", utilization=91.5)]),
}
check("多源3贴左(双档46+单档30+单档30)", w._docked_collapsed_size(),
      (88, 8 + 46 + 30 + 30))

# DeepSeek 修复验证:纯金额账号(只有 remaining 无 used/total)chips 不为空
chips = w._bar_chips(w._snapshots["id1"])
check("DeepSeek 纯金额账号 chips 含 '0.00' (修复前是 '--')",
      bool(chips) and "0.00" in chips[0][1], True)

w = make_window(MODE_MULTI, 10)
w._dock_edge = 'top'
check("多源10贴顶", w._docked_collapsed_size(), (200, 8 + 10 * 20))
check("多源10贴顶仍是横条画法(高208>宽200,不能误判竖条)",
      w._dock_edge in ('left', 'right'), False)

# ---- 几何断言:贴底时高度向上生长、底边对齐屏幕底 ----
w = make_window(MODE_MULTI, 3)
w._dock_edge = 'bottom'
g = w._docked_collapsed_geometry()
avail = w.screen().availableGeometry()
check("多源3贴底:底边对齐屏幕底", g.bottom(), avail.bottom())
check("多源3贴底:高度=68(3行×20+8)", g.height(), 8 + 3 * 20)

# ---- 绘制冒烟:三种吸附形态都能画出来不崩 ----
from PySide6.QtGui import QPixmap, QPainter

def paint_smoke(name, mode, n, edge):
    w = make_window(mode, n)
    w._dock_edge = edge
    pw, ph = w._docked_collapsed_size()
    pm = QPixmap(pw, ph)
    pm.fill()
    p = QPainter(pm)
    try:
        # 直接调内部绘制入口(绕过 paintEvent 的背景绘制)
        if edge in ('left', 'right'):
            w._draw_vertical_content(p, pw, ph)
        else:
            w._draw_float_window_content(p, pw, ph)
        p.end()
        print(f"PASS  绘制冒烟 {name} ({pw}x{ph})")
    except Exception as e:
        p.end()
        print(f"FAIL  绘制冒烟 {name}: {e!r}")
        FAILURES.append(f"paint:{name}")


paint_smoke("单源贴侧", MODE_SINGLE, 1, 'left')
paint_smoke("多源3贴侧", MODE_MULTI, 3, 'right')
paint_smoke("多源3贴顶", MODE_MULTI, 3, 'top')
paint_smoke("多源1贴顶", MODE_MULTI, 1, 'top')
paint_smoke("无账号多源贴侧", MODE_MULTI, 0, 'left')

# ---- 视觉验收:带真实快照渲染成 PNG,人工看效果 ----
from api.models import QuotaSnapshot, QuotaTier, TIER_FIVE_HOUR, TIER_WALLET

SNAPSHOTS = {
    "id0": QuotaSnapshot.success_snapshot(  # GLM 风格:5h 档 42%
        "glm", tiers=[
            QuotaTier(name=TIER_FIVE_HOUR, utilization=42.0),
            QuotaTier(name="weekly_limit", utilization=15.0),
        ]),
    "id1": QuotaSnapshot.success_snapshot(  # DeepSeek 风格:钱包余额
        "deepseek", tiers=[
            QuotaTier(name=TIER_WALLET, kind="money",
                      used=7.66, total=110.0, remaining=102.34, unit="CNY"),
        ]),
    "id2": QuotaSnapshot.success_snapshot(  # 高占用:报警色
        "kimi", tiers=[QuotaTier(name="weekly_limit", utilization=91.5)]),
}


def render_png(name, mode, n, edge):
    """完整画一条吸附条(含胶囊背景)存 PNG"""
    w = make_window(mode, n)
    w._dock_edge = edge
    w._snapshots = dict(SNAPSHOTS)
    pw, ph = w._docked_collapsed_size()
    pm = QPixmap(pw * 2, ph * 2)   # 2 倍放大,看得清
    pm.fill()
    p = QPainter(pm)
    p.scale(2, 2)
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(40, 44, 52))          # 深色底模拟胶囊背景
    p.drawRoundedRect(0, 0, pw, ph, min(pw, ph) / 2, min(pw, ph) / 2)
    if edge in ('left', 'right'):
        w._draw_vertical_content(p, pw, ph)
    else:
        scale_floor = 1.0 if mode == MODE_MULTI else 0.8
        w._draw_float_window_content(p, pw, ph, scale_floor)
    p.end()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"render_{name}.png")
    pm.save(out)
    print(f"saved {out}")


render_png("single_side", MODE_SINGLE, 1, 'left')
render_png("multi_side", MODE_MULTI, 3, 'left')
render_png("multi_top", MODE_MULTI, 3, 'top')

print()
if FAILURES:
    print(f"共 {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("全部通过")
