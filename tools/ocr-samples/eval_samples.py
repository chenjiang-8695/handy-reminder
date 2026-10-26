"""把 OCR 样本生成器（Mactarvish/ocr-sample-generator）的产物评估一遍。

为什么需要这个脚本 —— 两个坑都在"真值怎么拼"上：

1. 生成的图里每行是**随机位置**放置的，标注文件按插入顺序写，而 OCR 返回的是**阅读顺序**。
   直接按文件顺序拼真值，同一张图里多个时间点就会和 OCR 的"第一个时间"对不上，
   时间值准确率被冤枉成 87%。这里按每行四点框的 y1 排序后再拼。
2. 评估口径本身（空白不计错、编辑距离而非逐位比对）在 `ocr.evaluate()` 里，这个脚本只负责喂数据。

用法:
    python tools/ocr-samples/eval_samples.py C:/path/to/samples [--engine rapidocr]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from reminder_assistant import ocr  # noqa: E402


def read_truth(label_path: pathlib.Path) -> str:
    """按 y1 排序把每行拼成一段文本 —— 顺序必须跟 OCR 的阅读顺序一致。"""
    lines = []
    for raw in label_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or "###" not in raw:
            continue
        head, _, text = raw.partition("###")
        nums = [int(n) for n in head.split(",") if n.strip().lstrip("-").isdigit()]
        if len(nums) < 8:
            continue
        y1 = nums[1]
        x1 = nums[0]
        lines.append((y1, x1, text.rsplit(",", 1)[-1]))
    lines.sort(key=lambda item: (item[0], item[1]))
    return "".join(text for _, _, text in lines)


def collect(root: pathlib.Path, sub: str = "global_canvas") -> list[tuple[bytes, str]]:
    images = root / sub / "images"
    labels = root / sub / "labels"
    samples = []
    for img in sorted(images.glob("*.jpg")):
        label = labels / (img.stem + ".txt")
        if not label.is_file():
            continue
        samples.append((img.read_bytes(), read_truth(label)))
    return samples


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="生成器的输出目录 (含 global_canvas/)")
    ap.add_argument("--sub", default="global_canvas", help="子目录: global_canvas 或 rectified_lines")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 张的真值/识别对比")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    samples = collect(root, args.sub)
    if not samples:
        print(f"没找到样本: {root}")
        return 2

    engines = ocr.default_engines()
    if not engines:
        print("没有可用的 OCR 后端")
        return 3
    engine = engines[0]
    print(f"引擎: {type(engine).__name__} | 样本: {len(samples)} 张 | 目录: {root}")

    for png, truth in samples[: args.show]:
        got = engine.recognize(ocr.preprocess(png))
        print("--- 真值 ---")
        print(truth)
        print("--- 识别 ---")
        print(got)

    result = ocr.evaluate(engine, samples)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
