"""把测试图的**真实 OCR 输出**固化成 `tests/data/*.txt` (改了测试图之后跑一次)。

    python packaging/make_ocr_samples.py [图片目录]

默认读 `C:\\Users\\26756\\Desktop\\lingling\\测试图片` 下的四张图。用 Windows 自带 OCR
(纯命令行进程, 不建 Qt —— 反过来先建 Qt 再碰 winrt 会段错误, 见 reminder_assistant/ocr.py)。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reminder_assistant.ocr import WindowsOcrEngine, preprocess    # noqa: E402

DEFAULT_SRC = Path(r"C:\Users\26756\Desktop\lingling\测试图片")
OUT = ROOT / "tests" / "data"

# 图片名 -> 固化的文件名 (名字说明"这张图代表哪种现场", 用例里按它取)
SAMPLES: dict[str, str] = {
    "屏幕截图 2026-10-15 083315.png": "wechat_timestamp.txt",   # 含消息时间戳
    "屏幕截图 2026-10-15 083226.png": "wechat_am.txt",          # 写明上午
    "212d7dc894f908cba47766fd4d532fe0.jpg": "timetable.txt",    # 课表整屏时刻
    "e2dfaa2530d0228934e802da0da67601.jpg": "train.txt",        # 12306 车次列表
    "11f5ea3d79ec2dc2db6fb00c115ae480.jpg": "order_ticket.txt",  # 12306 订单详情(车票/过去的行程)
}


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else DEFAULT_SRC
    if not src.is_dir():
        print(f"图片目录不存在: {src}")
        return 2
    engine = WindowsOcrEngine()
    OUT.mkdir(parents=True, exist_ok=True)
    missing = []
    for image, name in SAMPLES.items():
        path = src / image
        if not path.is_file():
            missing.append(image)
            continue
        text = engine.recognize(preprocess(path.read_bytes(), scale=1))
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"{name}: {len(text.splitlines())} 行 / {len(text)} 字符")
    if missing:
        print("没找到这些图(其余已更新): " + "; ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
