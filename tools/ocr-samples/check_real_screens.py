"""真实截图的判定回归：这批图没有逐字标注，但"该建卡的建卡、不该建卡的不建卡"必须钉住。

为什么不用字符准确率衡量：客户投诉过的正是"车票/课表截图被误建成提醒"（V2.5.3）。
这类图全识别对也没意义，**多建一张卡就是 bug**。期望值写在 real-screens-expect.json。

用法:
    python tools/ocr-samples/check_real_screens.py "C:/Users/26756/Desktop/lingling/测试图片"
"""

from __future__ import annotations

import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

EXPECT = pathlib.Path(__file__).with_name("real-screens-expect.json")


def scan_image(path: pathlib.Path):
    """走与 `--ocr-image` 完全相同的那条链路, 但把结论结构化返回。"""
    from PIL import Image

    from reminder_assistant.ocr import (Scanner, Selection, StubOcrEngine,
                                        default_engines, probe_engines)

    probe_engines(force=True, allow_winrt=True)
    engines = [e for e in default_engines() if not isinstance(e, StubOcrEngine)]
    if not engines:
        raise RuntimeError("没有可用的 OCR 后端")

    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, format="PNG")
    png = buf.getvalue()
    return Scanner(engines=engines, capturer=lambda _bbox: png).scan(Selection(0, 0, 1, 1))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    folder = pathlib.Path(sys.argv[1])
    if not folder.is_dir():
        print(f"目录不存在: {folder}")
        return 2

    spec = json.loads(EXPECT.read_text(encoding="utf-8"))
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not images:
        print(f"没找到图片: {folder}")
        return 2

    failed = 0
    for img in images:
        want = spec.get(img.name)
        if not want:
            print(f"?? {img.name}: 期望表里没有这条, 跳过")
            continue
        res = scan_image(img)
        built = bool(res.focus) and res.parsed is not None and res.parsed.when is not None
        if want["expect"] == "no_card":
            ok = not built
            detail = "不建卡" if not built else f"误建卡「{res.focus}」"
        else:
            when = res.parsed.when if res.parsed else None
            expect_when = want.get("when")
            ok = built and (expect_when is None or
                            (when is not None and when.strftime("%Y-%m-%d %H:%M") == expect_when))
            detail = f"建卡 {when:%Y-%m-%d %H:%M}" if when else "没建卡"
        if not ok:
            failed += 1
        print(f"{'OK ' if ok else 'FAIL'} {img.name}: 期望 {want['expect']} / 实际 {detail}")

    print(f"\n合计 {len(images)} 张, 失败 {failed} 张")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
