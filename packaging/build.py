"""一键打包。

    python packaging/build.py                 # 生成图标/版本信息 -> PyInstaller -> 验证 -> 出 zip
    python packaging/build.py --no-zip        # 不压缩
    python packaging/build.py --check-only    # 只做产物自检, 不重新构建

产物:
    dist/HandyReminder/HandyReminder.exe      免安装目录
    dist/HandyReminder-<版本>-win64.zip       分发用压缩包

打包前请确认:
    1. 已经在目标解释器里装好 requirements.txt (+ 可选 rapidocr-onnxruntime 才有离线 OCR)
    2. 内置手写字体已放进 reminder_assistant/assets/fonts/ (否则打包出来的字体会退到系统楷体)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PKG))

from reminder_assistant import __version__  # noqa: E402

APP_NAME = "HandyReminder"
EXE = DIST / APP_NAME / f"{APP_NAME}.exe"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def prepare_assets() -> None:
    run([sys.executable, str(PKG / "make_icon.py")])
    run([sys.executable, str(PKG / "make_version_info.py")])
    fonts = sorted((ROOT / "reminder_assistant" / "assets" / "fonts").glob("*.[to]tf"))
    if fonts:
        print(f"内置字体: {', '.join(f.name for f in fonts)}")
    else:
        print("⚠ 未发现内置手写字体 (assets/fonts/*.ttf)。打包后字体会退到系统楷体,\n"
              "  PRD 4.1「内置加粗手写字体」这条验收将无法保证。")


def build() -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD),
         str(PKG / "handyreminder.spec")], cwd=str(ROOT))


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def static_verify() -> int:
    """不执行 exe 的结构校验。

    为什么必须有这一层: ①本机启用了 Smart App Control, 未签名的 exe 会被系统
    直接拒绝执行 (WinError 4551); ②CI 环境通常也没有桌面。缺任何一项都说明
    打包配置错了, 而这些问题在真机上表现为"双击没反应"或"时区全乱", 极难排查。
    """
    print("\n--- 结构校验 (不依赖执行) ---")
    bundle = DIST / APP_NAME
    internal = bundle / "_internal"
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {label}{('  ' + detail) if detail else ''}")

    check(EXE.exists(), "主程序存在", f"{EXE.stat().st_size / 1e6:.1f} MB" if EXE.exists() else "")
    check((bundle / "_internal").is_dir(), "onedir 结构 (_internal)")
    check((internal / "base_library.zip").exists(), "Python 运行时")
    check((internal / "sqlite3.dll").exists(), "sqlite3 (提醒持久化)")
    check((internal / "PySide6" / "Qt6Widgets.dll").exists(), "Qt6Widgets")
    check((internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll").exists(),
          "Qt 平台插件 qwindows (缺了窗口起不来)")
    check((internal / "assets" / "fonts").is_dir(), "内置字体目录 assets/fonts")
    # Windows 上 zoneinfo 靠 tzdata 包, 缺了时区/夏令时全崩
    check((internal / "tzdata" / "zoneinfo").is_dir(), "tzdata (时区/夏令时)")
    fonts = list((internal / "assets" / "fonts").glob("*.[to]tf"))
    print(f"  ! 内置字体文件: {len(fonts)} 个"
          + ("" if fonts else "  → PRD 4.1 的手写字体要求尚未满足, 见该目录 README"))
    # "装了就带上"必须真的带上: 离线 OCR 的模型权重不进去, 运行时就只能退到提示文案
    ocr_pkg = internal / "rapidocr_onnxruntime"
    if ocr_pkg.is_dir():
        models = list(ocr_pkg.rglob("*.onnx"))
        check(bool(models), "离线 OCR 模型 (.onnx)", f"{len(models)} 个")
    else:
        print("  ! 产物不含离线 OCR (没装 rapidocr-onnxruntime): 圈选识别会提示需要安装")

    # 版本资源: 只读文件属性, 不需要执行
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{EXE}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=60, errors="replace")
        got = (proc.stdout or "").strip()
        check(got.startswith(__version__), "版本资源", f"{got} (期望 {__version__})")
    except Exception as exc:                       # pragma: no cover
        check(False, "版本资源", f"读取失败: {exc}")
    return failures


def run_verify() -> tuple[int, bool]:
    """真的跑一遍 exe。返回 (失败数, 是否被系统策略拦截而跳过)。"""
    print("\n--- 执行校验 ---")
    has_ocr = (DIST / APP_NAME / "_internal" / "rapidocr_onnxruntime").is_dir()
    checks = [
        (["--version"], 0, "版本", None),
        (["--demo", "明天下午三点开会"], 0, "时间解析", None),
        (["--demo", "记得买牛奶"], 2, "无时间表达的退出码", None),
        (["--autostart", "status"], 0, "开机自启状态", None),
        (["--selftest"], 0, "端到端自检", None),
        # 窗口化构建没有自己的控制台, 帮助能不能打出来取决于 attach_parent_console 的时机
        (["--help"], 0, "帮助输出", "usage"),
        # 内置了 OCR 就必须真的认得出中文 (模型/DLL/cv2 缺一个都会在这里暴露);
        # 精简版产物允许它明确回一句"未内置"并退出 3
        (["--ocr-selftest"], 0 if has_ocr else 3, "离线 OCR", "OCR 自检"),
    ]
    failed = 0
    for args, want, label, expect_out in checks:
        try:
            proc = subprocess.run([str(EXE), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=180)
        except OSError as exc:
            winerr = getattr(exc, "winerror", None)
            if winerr == 4551 or "策略" in str(exc) or "policy" in str(exc).lower():
                print("  ! 本机启用了应用程序控制策略 (Smart App Control / WDAC),"
                      "\n    未签名的 exe 被系统禁止运行 —— 这不是构建问题, 结构校验已经通过。"
                      "\n    分发前必须做代码签名, 或在目标机上放行。"
                      "\n    自查: reg query \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy\" /v VerifiedAndReputablePolicyState")
                return failed, True
            raise
        ok = proc.returncode == want
        if ok and expect_out:
            ok = expect_out in (proc.stdout or "")
        failed += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {label:14s} exit={proc.returncode} (期望 {want})")
        for line in (proc.stdout or "").strip().splitlines()[:2]:
            print(f"      {line}")
    return failed, False


def verify() -> int:
    if not EXE.exists():
        print(f"✗ 找不到 {EXE}")
        return 1
    size_mb = dir_size(DIST / APP_NAME) / 1e6
    print(f"\n产物: {EXE}   目录 {size_mb:.0f} MB")

    failed = static_verify()
    run_failed, blocked = run_verify()
    failed += run_failed
    if blocked:
        print("\n  → 执行校验被跳过 (系统策略), 其余校验结果仍有效")
    return failed


def gui_check() -> int:
    """真的把窗口起一次。

    结构校验和 `--selftest` 都证明不了"窗口能不能起来": Qt 平台插件、被排除的 Qt 模块、
    内置字体读取, 只有真正建出窗口时才走得到, 而它们的故障表现都是"双击没反应"。
    """
    print("\n--- GUI 冒烟 ---")
    if not EXE.exists():
        print("  ✗ 找不到 exe, 跳过")
        return 1
    try:
        from smoke_gui import smoke
    except ImportError as exc:                      # pragma: no cover
        print(f"  ✗ 无法加载冒烟脚本: {exc}")
        return 1
    ok, detail = smoke(EXE)
    print(f"  {'✓' if ok else '✗'} {detail}")
    return 0 if ok else 1


def find_iscc() -> Path | None:
    """找 Inno Setup 的编译器。装在 Program Files 或用户目录都认。"""
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            cand = Path(base) / ("Programs/Inno Setup 6/ISCC.exe" if base.endswith("AppData\\Local")
                                 else "Inno Setup 6/ISCC.exe")
            if cand.is_file():
                return cand
    return None


def make_installer() -> int:
    """编译安装包。没装 Inno Setup 时给出可操作的提示, 而不是报错退出。"""
    iscc = find_iscc()
    if iscc is None:
        print("\n跳过安装包: 没找到 Inno Setup 6 (ISCC.exe)。\n"
              "  安装: winget install --id JRSoftware.InnoSetup -e\n"
              "  然后: python packaging/build.py --installer")
        return 0
    print(f"\n--- 编译安装包 ({iscc}) ---")
    run([str(iscc), str(PKG / "installer.iss"), f"/DAppVersion={__version__}"], cwd=str(ROOT))
    out = PKG / "out" / f"{APP_NAME}-{__version__}-setup.exe"
    if not out.is_file():
        print("  ✗ 安装包没有生成")
        return 1
    print(f"  ✓ {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print("    注意: 未签名的安装包在启用 Smart App Control 的机器上会被系统拦 (WinError 4551);")
    print("          正式分发需要代码签名证书, 本机自测可用自签名证书。")
    return 0


def make_zip() -> Path:
    target = DIST / f"{APP_NAME}-{__version__}-win64.zip"
    src = DIST / APP_NAME
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, Path(APP_NAME) / f.relative_to(src))
    print(f"\n分发压缩包: {target}  ({target.stat().st_size / 1e6:.0f} MB)")
    print("用户解压后直接双击 HandyReminder.exe; 开机自启在托盘菜单里勾选。")
    return target


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-zip", action="store_true")
    p.add_argument("--check-only", action="store_true", help="只跑产物自检")
    p.add_argument("--zip-only", action="store_true", help="只对已有产物重新打压缩包")
    p.add_argument("--gui-check", action="store_true",
                   help="额外做一次 GUI 冒烟 (会短暂弹出便签窗口, 用来验证被排除的 Qt 模块没伤到窗口)")
    p.add_argument("--installer", action="store_true",
                   help="编译 Inno Setup 安装包 (需要已装 Inno Setup 6)")
    args = p.parse_args()

    if args.zip_only:
        make_zip()
        return 0

    if not args.check_only:
        prepare_assets()
        build()

    failed = verify()
    if args.gui_check:
        failed += gui_check()
    if failed:
        print(f"\n✗ {failed} 项自检未通过")
        return 1
    if not args.no_zip and not args.check_only:
        make_zip()
    if args.installer:
        failed += make_installer()
    if failed:
        print(f"\n✗ 安装包环节失败 ({failed} 项)")
        return 1
    print("\n✓ 打包完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
