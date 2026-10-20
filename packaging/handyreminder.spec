# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

目标: 一个免安装的目录式产物 dist/HandyReminder/HandyReminder.exe。
  * 目录式而不是单文件: 单文件每次启动都要把 ~200MB 解包到临时目录, 启动慢好几秒,
    而且自启项指向临时目录容易被安全软件当成可疑行为清理 (第 10 项踩过)。
  * console=False: 开机自启时不能闪黑框; 需要 CLI 时由 winconsole.attach_parent_console()
    在运行时附着父控制台。
  * OCR 后端按"装了就带上"处理, 没装也能跑 (会给出明确的安装提示), 所以同一份 spec
    既能出精简版也能出完整离线 OCR 版。
"""

import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (collect_all, collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 - SPECPATH 由 PyInstaller 注入
sys.path.insert(0, str(ROOT))

APP_NAME = "HandyReminder"
ENTRY = str(ROOT / "packaging" / "entrypoint.py")
ICON = str(ROOT / "packaging" / "assets" / "icon.ico")
VERSION_FILE = str(ROOT / "packaging" / "version_info.txt")

datas = [(str(ROOT / "reminder_assistant" / "assets"), "assets")]
binaries = []
hiddenimports = []
OPTIONAL = []


def _optional(pkg: str, collect: str | None = None) -> bool:
    if importlib.util.find_spec(pkg) is None:
        return False
    OPTIONAL.append(pkg.split(".")[0])
    if collect:
        d, b, h = collect_all(collect)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
    return True


# 离线 OCR: 首选 RapidOCR (PP-OCR/onnxruntime), 备选 Windows 自带 OCR
_optional("rapidocr_onnxruntime", "rapidocr_onnxruntime")
_optional("winsdk", "winsdk")
# Windows 自带 OCR 的新式投影包 (winrt-*, Python 3.13/3.14 上还能装, winsdk 装不上)。
# 它们是**一堆独立分发**: winrt-runtime / winrt-Windows.Media.Ocr / ... .Graphics.Imaging /
# ...Storage.Streams / ...Globalization / ...Foundation / ...Foundation.Collections。
# 收法有坑: `winrt` 是**命名空间包**(没有 __init__.py), 投影体是 winrt/_winrt_windows_*.pyd,
# 由 winrt.runtime 的自定义 finder 在运行时注册成 winrt.windows.* —— 只 collect_submodules
# 会漏掉子包 (实测产物里 `import winrt.windows.media` 报 No module named), 所以要
# 子模块 + 动态库 + 数据文件三件套全上, 把整棵 winrt/ 树都带进去。
if importlib.util.find_spec("winrt") is not None:
    OPTIONAL.append("winrt")
    hiddenimports.extend(collect_submodules("winrt"))
    binaries.extend(collect_dynamic_libs("winrt"))
    datas.extend(collect_data_files("winrt", include_py_files=True,
                                    excludes=["**/__pycache__/**"]))
# 截图与图像预处理
_optional("mss")
_optional("PIL", "PIL")
_optional("win11toast", "win11toast")

excludes = [
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook", "jupyter",
    "PyQt5", "PyQt6", "PySide2", "wx", "pytest", "numpy.f2py", "setuptools._distutils",
    "torch", "tensorflow", "onnxruntime.tools", "onnxruntime.quantization",
]

# ---------------------------------------------------------------- 瘦身 (V2.2)
#
# 原则: 每一项排除都必须用 packaging/build.py 的 5 项执行校验 + packaging/smoke_gui.py
# (真的起窗口) 复验一次。被排掉的 Qt 模块如果其实是别的模块的依赖, 故障表现是"双击没反应",
# 结构校验完全看不出来 —— 所以是"排除一组 -> 重建 -> 跑校验 + GUI 冒烟"这样一组一组往前走。
HAVE_RAPIDOCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None
# 纯 QWidget 程序 (QPainter 光栅绘制, 没有 QML/Quick/QOpenGLWidget) 用不到软件 OpenGL 兜底;
# 若目标机是远程桌面/无显卡驱动的环境且窗口起不来, 用这个开关把它加回来即可。
KEEP_SOFT_OPENGL = os.environ.get("HANDYREMINDER_KEEP_SOFT_OPENGL") == "1"

# PySide6 的 Qt 模块依赖表很宽 (QtWidgets 的元数据里挂着 QtOpenGL/QtSvg/QtPrintSupport...),
# 靠排除 Python 子模块 + 过滤 DLL 两层才能真的把它们去掉。
_UNUSED_QT_MODULES = (
    "QtQml", "QtQuick", "QtQuickWidgets", "QtQuickControls2", "QtQmlModels",
    "QtPdf", "QtPdfWidgets", "QtNetwork", "QtOpenGL", "QtOpenGLWidgets",
    "QtSvg", "QtSvgWidgets", "QtPrintSupport", "QtSql", "QtXml", "QtTest",
    "QtHelp", "QtUiTools", "QtDesigner", "QtMultimedia", "QtWebEngineCore",
    "QtWebEngineWidgets", "QtWebChannel", "QtWebSockets", "QtCharts",
    "QtDataVisualization", "QtGraphs", "QtBluetooth", "QtPositioning",
    "QtSensors", "QtSerialPort", "QtStateMachine", "QtScxml", "Qt3DCore",
    "Qt3DRender", "Qt3DInput", "Qt3DAnimation", "Qt3DExtras", "QtVirtualKeyboard",
    "QtHttpServer", "QtDBus", "QtConcurrent", "QtNfc", "QtRemoteObjects",
    "QtSpatialAudio", "QtTextToSpeech", "QtLocation", "QtPdfQuick", "QtQuick3D",
)
excludes += [f"PySide6.{m}" for m in _UNUSED_QT_MODULES]
if not HAVE_RAPIDOCR:
    # numpy 只被 RapidOcrEngine.recognize 延迟导入; 产物里没有 RapidOCR 时它是纯负担
    # (numpy 本体 6MB + 配套的 OpenBLAS 20MB —— 全产物最重的一块)。
    excludes.append("numpy")

# 连同 DLL 一起过滤 (hook 已经把它们复制进来了, excludes 管不到二进制)。
# 注意模块名到 DLL 名的换算: PySide6 的模块叫 QtQml, 对应的库是 Qt6Qml.dll (去掉 "Qt" 换成 "Qt6")。
_DROP_BINARIES = tuple(f"Qt6{m[2:]}" for m in _UNUSED_QT_MODULES) + (
    "libcrypto-3-x64", "libssl-3-x64",       # 只有 Qt6Network 的 TLS 后端会用
    "Qt6VirtualKeyboard",
)
if not KEEP_SOFT_OPENGL:
    _DROP_BINARIES += ("opengl32sw",)
# Pillow 里我们不用的解码器: 只开 PNG (preinit 内置), avif/webp 的解码器是延迟导入的独立 .pyd
_DROP_PIL = ("_avif", "_webp")
# OpenCV 的 ffmpeg 视频库 (31MB): 只处理静态图, 用不到 VideoCapture/VideoWriter。
# 它由 OpenCV 在用到视频接口时才按需加载, 所以删掉不影响 cvtColor/resize 这些图像运算 ——
# 但这类"删了到底影不影响"的判断必须由产物自己验证: 构建后跑 `--ocr-selftest`。
_DROP_CV = ("opencv_videoio_ffmpeg",)
_DROP_PLUGIN_DIRS = ("tls", "networkinformation", "platforminputcontexts", "generic")
_KEEP_PLATFORMS = {"qwindows.dll"}                      # 只需要 Windows 平台插件
_KEEP_IMAGEFORMATS = {"qico.dll"}                       # 图标都是画出来的, 不读图片文件
# Qt 自带 100 多个翻译文件 (6.8MB), 只有中文界面用得上
_KEEP_TRANSLATIONS = {"qt_zh_CN.qm", "qtbase_zh_CN.qm", "qt_help_zh_CN.qm",
                      "qt_en.qm", "qtbase_en.qm"}


def _slim(entries: list, what: str) -> list:
    """按目标路径过滤 PyInstaller 的 binaries / datas 条目, 并报告省了多少。"""
    kept, dropped_bytes = [], 0
    for dest, src, kind in entries:
        norm = str(dest).replace("\\", "/")
        name = norm.rsplit("/", 1)[-1]
        parts = norm.split("/")
        drop = any(name.startswith(f"{d}.dll") or name.startswith(f"{d}.") for d in _DROP_BINARIES)
        drop = drop or any(name.startswith(p) for p in _DROP_PIL)
        drop = drop or any(name.startswith(p) for p in _DROP_CV)
        if "plugins" in parts and "PySide6" in parts:
            sub = parts[parts.index("plugins") + 1] if parts.index("plugins") + 1 < len(parts) else ""
            drop = drop or sub in _DROP_PLUGIN_DIRS
            drop = drop or (sub == "platforms" and name not in _KEEP_PLATFORMS)
            drop = drop or (sub == "imageformats" and name not in _KEEP_IMAGEFORMATS)
        if "/translations/" in norm and name.endswith(".qm") and name not in _KEEP_TRANSLATIONS:
            drop = True
        if drop:
            try:
                dropped_bytes += os.path.getsize(src)
            except OSError:
                pass
        else:
            kept.append((dest, src, kind))
    print(f"[spec] 瘦身: {what} {len(entries)} -> {len(kept)} 项, 省下 {dropped_bytes / 1e6:.1f} MB")
    return kept

a = Analysis(                                       # noqa: F821
    [ENTRY],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
# 排除清单只拦得住 Python 模块; 已被 hook 复制进来的 Qt DLL / 插件 / 翻译文件要在这里过滤,
# 过滤的正确性由 build.py 的执行校验 + smoke_gui.py 的"真起窗口"兜住。
a.binaries = _slim(a.binaries, "binaries")          # noqa: F821
a.datas = _slim(a.datas, "datas")                   # noqa: F821

pyz = PYZ(a.pure)                                   # noqa: F821

exe = EXE(                                          # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX 会让部分安全软件误报, 得不偿失
    console=False,                 # 无黑框; CLI 靠运行时附着父控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)

coll = COLLECT(                                     # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

print(f"[spec] 已并入的可选依赖: {', '.join(OPTIONAL) or '无 (精简版)'}")
