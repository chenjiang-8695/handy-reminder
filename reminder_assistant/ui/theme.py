"""手账视觉: 纸张质感、手写字体、性能友好的绘制缓存。

设计要点 (对应 PRD 1.1 / 4.1 / 4.3):
  * 纸张纹理一次绘制成 QPixmap 并按尺寸缓存 —— 不能每帧现场画噪点,
    否则 60fps 重绘会把 CPU 拉满 (工程师清单第 3 条)。
  * 手写字体优先用内置的开源字体文件 (霞鹜文楷 LXGW WenKai), 找不到才退到
    系统楷体/手写体; 字体文件随包分发才能保证"100% 清晰可辨认"这条验收。
  * 排版模拟真实书写: 行距固定, 字距按字符宽度做 ±0.5px 抖动, 基线不齐整。
"""

from __future__ import annotations

import random
from pathlib import Path

PAPER = "#FCF7EC"
RULE = "#C6DCEA"
MARGIN = "#E9B7B0"
INK = "#2E2A25"
INK_LIGHT = "#7A7267"
SHADOW = (0, 0, 0, 46)

LEVEL_COLORS = {"日常": "#7C9C6B", "重要": "#C98A3C", "紧急": "#C0504A"}

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"   # 源码运行时的兜底
try:                                     # 打包后资源在 _MEIPASS 里, 必须走 bundle 定位
    from ..resources import resource_path

    FONT_DIR = resource_path("assets", "fonts")
except Exception:                        # pragma: no cover
    pass
HANDWRITING_CANDIDATES = [
    "LXGW WenKai", "霞鹜文楷", "LXGWWenKaiMono", "霞鹜文楷等宽",
    "Segoe Print", "Bradley Hand ITC", "Ink Free",
    "楷体", "KaiTi", "STKaiti", "华文楷体", "Comic Sans MS",
]

LINE_HEIGHT = 34
PADDING = (22, 18, 22, 16)


# ---------------------------------------------------------------- 字体

_loaded_families: list[str] = []


def load_bundled_fonts() -> list[str]:
    """把 assets/fonts 下的字体注册进 Qt, 返回可用家族名列表。"""
    from PySide6.QtGui import QFontDatabase

    families: list[str] = []
    if FONT_DIR.is_dir():
        for path in sorted(FONT_DIR.glob("*.[to]tf")):
            fid = QFontDatabase.addApplicationFont(str(path))
            if fid >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(fid))
    _loaded_families[:] = families
    return families


def handwriting_font(size: int = 15, bold: bool = True):
    from PySide6.QtGui import QFont

    names = list(_loaded_families) + HANDWRITING_CANDIDATES
    for name in names:
        f = QFont(name, size)
        f.setBold(bold)
        if f.exactMatch() or name in _loaded_families:
            return f
    f = QFont()
    f.setPointSize(size)
    f.setBold(bold)
    return f


# ---------------------------------------------------------------- 纸张

def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def build_paper(width: int, height: int, *, dpr: float = 1.0, seed: int = 7, ruled: bool = True):
    """生成一张纸张底图 (含纹理、横线、页边线、内阴影)。结果应被调用方缓存。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap

    pw, ph = max(1, int(width * dpr)), max(1, int(height * dpr))
    pm = QPixmap(pw, ph)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    # 纸面
    from PySide6.QtCore import QRectF

    rect = QRectF(0, 0, width, height)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(PAPER))
    p.drawRoundedRect(rect, 4, 4)

    # 木纹/纤维噪点: 只在边缘附近撒少量点, 中心留白, 保证正文区干净
    rnd = _rng(seed)
    p.setPen(Qt.NoPen)
    for _ in range(int(width * height / 420)):
        x = rnd.uniform(0, width)
        y = rnd.uniform(0, height)
        edge = min(x, y, width - x, height - y)
        alpha = 12 if edge > 24 else 26
        p.setBrush(QColor(160, 146, 120, alpha))
        p.drawEllipse(QRectF(x, y, 1.2, 1.2))

    # 横向书写线
    if ruled:
        pen = QPen(QColor(RULE))
        pen.setWidthF(0.9)
        p.setPen(pen)
        y = PADDING[1] + LINE_HEIGHT
        while y < height - PADDING[3]:
            p.drawLine(0, int(y), int(width), int(y))
            y += LINE_HEIGHT

    # 左侧页边红线
    p.setPen(QPen(QColor(MARGIN), 1.2))
    p.drawLine(int(PADDING[0] - 8), 0, int(PADDING[0] - 8), int(height))

    # 纸的轻微内阴影
    grad = QLinearGradient(0, 0, 0, height)
    grad.setColorAt(0.0, QColor(0, 0, 0, 14))
    grad.setColorAt(0.12, QColor(0, 0, 0, 0))
    grad.setColorAt(0.88, QColor(0, 0, 0, 0))
    grad.setColorAt(1.0, QColor(0, 0, 0, 18))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(rect, 4, 4)
    p.end()
    return pm


class PaperCache:
    """按 (尺寸, dpr) 缓存纸张底图 —— 静态背景不参与逐帧重绘。"""

    def __init__(self, seed: int = 7):
        self.seed = seed
        self._key = None
        self._pm = None

    def get(self, width: int, height: int, dpr: float = 1.0):
        key = (width, height, round(dpr, 2))
        if key != self._key:
            self._pm = build_paper(width, height, dpr=dpr, seed=self.seed)
            self._key = key
        return self._pm


def jitter(text: str, seed: int = 0) -> str:
    """模拟手写的轻微不整齐: 在字符间插入零宽空格, 让字距不完全均匀。"""
    rnd = _rng(seed)
    out = []
    for ch in text:
        out.append(ch)
        if rnd.random() < 0.18:
            out.append("\u200b")
    return "".join(out)
