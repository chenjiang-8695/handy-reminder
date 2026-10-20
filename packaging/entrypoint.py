"""PyInstaller 入口脚本。

不能直接拿 reminder_assistant/__main__.py 当入口: 里面有相对导入 (`from .app import ...`),
被当成顶层脚本运行时会 ImportError。这里只做绝对导入 + freeze_support。
"""

from __future__ import annotations

import multiprocessing
import sys

from reminder_assistant.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()      # 万一将来用到多进程, 不写这句会无限开新进程
    sys.exit(main())
