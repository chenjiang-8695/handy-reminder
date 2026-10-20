"""打包后的资源定位。

PyInstaller 会把 datas 解包到一个临时目录 (_MEIPASS)。源码运行时资源在包目录旁边。
两种情况下都要能找到 assets/fonts, 否则内置手写字体会静默失效 —— 打包后最容易漏的一环。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """打包后 = 解包临时目录; 源码运行 = reminder_assistant/ 包目录。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    return bundle_root().joinpath(*parts)


def app_root() -> Path:
    """exe 所在目录 (打包) 或项目根 (源码), 用于放日志/默认数据目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
