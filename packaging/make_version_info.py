"""生成 Windows 版本信息资源 (PyInstaller --version-file 的格式)。

从 reminder_assistant.__version__ 生成, 而不是手写一份 —— 手写的迟早和代码里的版本不一致,
而"关于"对话框里的版本号对不上是最容易让客户失去信任的一类小问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reminder_assistant import __version__  # noqa: E402

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={ver_tuple},
    prodvers={ver_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', '{company}'),
        StringStruct('FileDescription', '{desc}'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'HandyReminder'),
        StringStruct('LegalCopyright', '{copyright}'),
        StringStruct('OriginalFilename', 'HandyReminder.exe'),
        StringStruct('ProductName', '智能手账式悬浮提醒助手'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def version_tuple(v: str) -> str:
    parts = [int(x) for x in v.split(".") if x.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return "(" + ", ".join(str(p) for p in parts[:4]) + ")"


def render(version: str = __version__, company: str = "待填: 公司名",
           copyright_: str = "© 2026 待填: 公司名") -> str:
    return TEMPLATE.format(ver_tuple=version_tuple(version), version=version,
                           company=company, copyright=copyright_,
                           desc="智能手账式悬浮提醒助手")


def main(out: Path | None = None) -> int:
    target = out or (Path(__file__).resolve().parent / "version_info.txt")
    target.write_text(render(), encoding="utf-8")
    print(f"已生成 {target}  (版本 {__version__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
