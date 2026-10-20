"""生成应用图标 (packaging/assets/icon.ico)。

用代码画而不是塞一个来路不明的二进制: 可复现、可改、可评审。
设计上按 16px 的可辨识度倒推 —— 小尺寸下只有"纸张 + 几道横线 + 一个高亮元素"能看清,
所以不加细节。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "assets"
SIZES = [256, 128, 64, 48, 32, 24, 16]
PAPER = (252, 247, 236, 255)
BORDER = (201, 191, 168, 255)
RULE = (198, 220, 234, 255)
MARGIN = (233, 183, 176, 255)
DONE = (124, 156, 107, 255)
# INK 不再使用: 黑点在图标里含义不明, 已换成对勾


def draw(size: int) -> Image.Image:
    s = size
    k = s / 256.0                       # 按 256 基准等比缩放
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = max(1, round(10 * k))
    radius = max(2, round(30 * k))
    d.rounded_rectangle([pad, pad, s - pad - 1, s - pad - 1],
                        radius=radius, fill=PAPER,
                        outline=BORDER, width=max(1, round(7 * k)))

    left = pad + round(22 * k)
    right = s - pad - round(22 * k)
    # 左侧页边红线: 上下都要让开圆角, 否则会从圆角处戳出去
    d.line([left - round(10 * k), pad + radius + round(6 * k),
            left - round(10 * k), s - pad - radius - round(6 * k)],
           fill=MARGIN, width=max(1, round(6 * k)))

    # 两行普通书写线 + 一行"已完成"的绿色短杠(带对勾)
    for y in (pad + round(56 * k), pad + round(96 * k)):
        d.line([left, y, right, y], fill=RULE, width=max(1, round(13 * k)))

    y3 = pad + round(136 * k)
    end = right - round(78 * k)
    d.line([left, y3, end, y3], fill=DONE, width=max(1, round(15 * k)))

    # 对勾: 比一个含义不明的黑点更明确地表达"提醒/待办已完成"
    cx, cy = end + round(14 * k), y3 - round(2 * k)
    w = max(1, round(11 * k))
    d.line([cx - round(12 * k), cy, cx - round(3 * k), cy + round(9 * k)], fill=DONE, width=w)
    d.line([cx - round(3 * k), cy + round(9 * k), cx + round(14 * k), cy - round(13 * k)],
           fill=DONE, width=w)
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    imgs = [draw(s) for s in SIZES]
    ico = OUT / "icon.ico"
    imgs[0].save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    imgs[0].save(OUT / "icon.png", format="PNG")
    print(f"已生成 {ico}  ({ico.stat().st_size} bytes, {len(SIZES)} 种尺寸)")
    for s in SIZES:
        draw(s).save(OUT / f"icon-{s}.png", format="PNG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
