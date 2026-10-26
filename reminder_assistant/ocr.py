"""圈选截图 + 多字体 OCR + 时间语义抽取 (PRD 2.1)。

链路: 半透明冻结层上鼠标圈选 -> 物理像素 bbox -> 截图 -> 预处理 -> OCR 融合
      -> timeparse 抽时间 -> 生成卡片 (或匹配到已有卡片则删除)

关于"必须能识别不同风格字体"这条验收标准, 说清工程现实:
  * 印刷体 (宋/黑/楷/圆体, 网页/PDF/截图) —— Windows.Media.Ocr 与 PP-OCR 都能到 95%+
  * 书法体/艺术字 —— 需要 PP-OCRv4/v5 的 rec 模型, 通用模型掉点到 60-80%
  * 手写体 —— 只有专门的手写模型 (如 PaddleOCR 手写数据集微调) 才有可用准确率;
    Windows 自带 OCR 对手写基本不可用。因此默认走 PP-OCR 融合, 并把
    "准确率" 做成可量化的 evaluate() 而不是拍脑袋承诺 100%。
"""

from __future__ import annotations

import difflib
import io
import logging
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from .models import Reminder
from .pickline import pick_line
from .timeparse import ParseResult, parse_time

log = logging.getLogger("reminder.ocr")


# ---------------------------------------------------------------- 数据

@dataclass
class Selection:
    """用户在冻结层上圈出的区域 (物理像素)。"""

    x: int
    y: int
    w: int
    h: int

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)


@dataclass
class ScanResult:
    text: str
    parsed: ParseResult | None
    bbox: tuple[int, int, int, int]
    engine: str = ""
    confidence: float = 0.0
    # 圈划到已有提醒 -> 走删除而不是新建
    matched_reminders: list[Reminder] = field(default_factory=list)
    # 整段识别文本里**真正用来建卡的那一行** (客户反馈: 抗干扰太弱 —— 圈到聊天记录时
    # 会把上方的时间戳也一起当日程)。建卡标题用 focus, 不用整段 text。
    focus: str = ""
    # 有候选但全被判成时间戳/时刻表 -> 不建卡, 交给界面说"这段里没读到像日程的话"
    rejected: str = ""

    @property
    def action(self) -> str:
        if self.matched_reminders:
            return "delete"
        return "create" if self.parsed and self.parsed.when else "manual"


# ---------------------------------------------------------------- 引擎

class OcrEngine(Protocol):
    name: str

    def recognize(self, png: bytes, *, lang: str = "chi_sim+eng") -> str:
        ...


class StubOcrEngine:
    """没有可用引擎时的守门员: 明确报错, 而不是静默返回空字符串。"""

    name = "stub"

    def recognize(self, png: bytes, *, lang: str = "chi_sim+eng") -> str:
        raise RuntimeError(
            "当前解释器里没有 OCR 后端。一句命令装好: "
            "python -m reminder_assistant --install-ocr "
            "(或手动 pip install rapidocr-onnxruntime / mss + Windows 自带 OCR)")


class WindowsOcrEngine:
    """Windows 自带 OCR (Windows.Media.Ocr): 离线、快 (约 20-60ms/次), 中英文混排好, 手写差。

    两个投影包都认: 新的 `winrt-*` 分包 (Python 3.13+ 上还能装) 和旧的 `winsdk`。
    它的价值是**不需要模型** —— 少一个几百 MB 的 onnxruntime 也能有可用的圈选识别。
    """

    name = "windows-ocr"

    def __init__(self):
        self._asyncio = __import__("asyncio")
        self._imaging, self._ocr, self._streams, self._g = self._import_projections()
        langs = []
        try:
            langs = [lang.language_tag for lang in
                     self._ocr.OcrEngine.available_recognizer_languages]
        except Exception:                            # noqa: BLE001 - 查不到就算了
            pass
        self.languages = langs
        if langs and not any(l.lower().startswith(("zh", "en")) for l in langs):
            raise RuntimeError(f"Windows OCR 没装中文/英文语言包 (现有: {', '.join(langs)})")

    @staticmethod
    def _import_projections():
        errors = []
        for prefix in ("winrt.", "winsdk."):
            try:
                return (__import__(prefix + "windows.graphics.imaging", fromlist=["x"]),
                        __import__(prefix + "windows.media.ocr", fromlist=["x"]),
                        __import__(prefix + "windows.storage.streams", fromlist=["x"]),
                        __import__(prefix + "windows.globalization", fromlist=["x"]))
            except Exception as exc:                 # noqa: BLE001 - 换下一个命名空间再试
                errors.append(f"{prefix}: {exc}")
        raise RuntimeError("没装 Windows OCR 投影包 (pip install winrt-Windows.Media.Ocr "
                           "winrt-Windows.Graphics.Imaging winrt-runtime); " + "; ".join(errors))

    def recognize(self, png: bytes, *, lang: str = "zh-Hans-CN") -> str:
        return self._asyncio.run(self._recognize(png, lang))

    async def _recognize(self, png: bytes, lang: str) -> str:  # pragma: no cover - 需 Windows
        stream = self._streams.InMemoryRandomAccessStream()
        writer = self._streams.DataWriter(stream)
        writer.write_bytes(png)
        await writer.store_async()
        decoder = await self._imaging.BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = None
        try:
            engine = self._ocr.OcrEngine.try_create_from_language(self._g.Language(lang))
        except Exception:                            # noqa: BLE001 - 语言标签不被认就退回系统默认
            engine = None
        if engine is None:
            engine = self._ocr.OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("Windows OCR 起不来 (系统里没有可用的 OCR 语言)")
        result = await engine.recognize_async(bitmap)
        # Windows OCR 给的中文是**逐字用空格分开**的 ("明 天 下 午 三 点 开 会") ——
        # 直接返回的话时间解析会认不出来("明天下午三点"被空格切碎), 卡片就建不出来。
        lines = []
        for line in getattr(result, "lines", None) or []:
            words = [w.text for w in getattr(line, "words", None) or []]
            lines.append(join_fragments(words))
        if lines:
            return "\n".join(lines)
        return result.text


def join_fragments(parts: list[str]) -> str:
    """把分词结果拼回一句话: 中日韩之间不补空格, 拉丁/数字之间补。"""
    out = ""
    for i, part in enumerate(parts):
        part = (part or "").strip()
        if not part:
            continue
        if i and out and _wordish(out[-1]) and _wordish(part[0]):
            out += " "
        out += part
    return out


class RapidOcrEngine:
    """RapidOCR (PP-OCR onnxruntime): 离线、无 torch、中文与常见印刷体效果最好。"""

    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def recognize(self, png: bytes, *, lang: str = "ch") -> str:
        import numpy as np
        from PIL import Image

        img = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
        result, _ = self._engine(img)
        if not result:
            return ""
        return "\n".join(reading_order([(item[0], str(item[1])) for item in result]))


def reading_order(quads: list[tuple[list, str]]) -> list[str]:
    """把 OCR 返回的框图按**阅读顺序**排好 (先上下、再左右), 并拼成行。

    为什么必须自己排: 引擎返回的顺序是检测顺序, 实测同一行会被切成好几段、还会乱序
    (例如"明天" 排在 "三点开会" 后面) —— 而识别结果直接当卡片标题用, 顺序错了用户看到的就是
    "三点开会明天"这种句子。

    之前这里用的键是 `min(y) // 12` (硬编码的像素桶): 预处理会把图放大两倍, 桶尺寸不变,
    于是行分组整个错位。改成按"行中心 y 相近 + 框高比例"聚类, 跟缩放无关。
    """
    boxes: list[tuple[float, float, float, float, str]] = []
    for quad, text in quads:
        try:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
        except (TypeError, ValueError, IndexError):
            continue
        if not text:
            continue
        boxes.append((min(ys), max(ys), min(xs), max(xs), text))
    if not boxes:
        return []

    boxes.sort(key=lambda b: (b[0] + b[1]) / 2)
    lines: list[list[tuple[float, float, float, float, str]]] = []
    for box in boxes:
        center = (box[0] + box[1]) / 2
        for line in lines:
            ref = line[0]
            height = max(ref[1] - ref[0], box[1] - box[0], 1.0)
            if abs(center - (ref[0] + ref[1]) / 2) <= height * 0.6:   # 同一行
                line.append(box)
                break
        else:
            lines.append([box])

    out: list[str] = []
    for line in lines:
        line.sort(key=lambda b: b[2])                     # 行内从左到右
        out.append(_merge_fragments(line))
    return out


def _merge_fragments(line: list[tuple[float, float, float, float, str]]) -> str:
    """拼同一行的若干段, 并**去掉重叠检测造成的重复字**。

    实测 (预处理放大两倍后): 一句话被切成三段, 且相邻段的框在横向上重叠 ~60px ——
    重叠的那部分对应的字被认了两遍, 直接拼接会得到"明天天下午三三点开会"。
    框重叠时按"最长公共后缀=前一段的尾巴 / 后一段的开头"吃掉重复的字。
    """
    text = ""
    prev: tuple[float, float, float, float, str] | None = None
    for box in line:
        frag = box[4]
        if prev is not None:
            gap = box[2] - prev[3]
            if gap < 0:                                   # 横向重叠 -> 去重
                frag = frag[_overlap_chars(text, frag):]
            elif _needs_space(prev, box):
                text += " "
        text += frag
        prev = box
    return text


def _overlap_chars(left: str, right: str, cap: int = 4) -> int:
    """left 的结尾与 right 的开头重复了几个字 (最多 cap 个)。"""
    limit = min(len(left), len(right), cap)
    for k in range(limit, 0, -1):
        if left[-k:] == right[:k]:
            return k
    return 0


def _needs_space(prev: tuple, cur: tuple) -> bool:
    """两段之间要不要补空格。

    中日韩文字之间本来就不留空格, 而两段中文中间断开多半是"某个字没认出来" ——
    补个空格反而把句子读断了 (实测"明天 三点开会")。只有**拉丁字母/数字**之间断开很远时才补。
    """
    gap = cur[2] - prev[3]
    width = max(1.0, cur[3] - cur[2])
    if gap <= max(6.0, width * 0.4):
        return False
    return _wordish(prev[4][-1]) and _wordish(cur[4][0])


def _wordish(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum())


def default_engines() -> list[OcrEngine]:
    """按"装了什么用什么"的顺序拼装; 全都不可用则返回 Stub 以便报错。

    结果**进程内缓存** (构造引擎要加载几十 MB 模型): 界面判断"有没有后端"、预热、真正识别
    都拿同一份结果, 不会出现"判定说有、真用的时候说没有"这种自相矛盾的情况。
    """
    engines, _notes = probe_engines()
    return engines


# 能提供 OCR 的包 -> 给人看的名字。用来做**廉价**的预检:
# 构造引擎要加载几十 MB 的模型 (几秒), 而"有没有后端"只需要看一眼包在不在。
_BACKENDS: tuple[tuple[str, str], ...] = (
    ("rapidocr_onnxruntime", "rapidocr"),
    ("winrt_windows.media.ocr", "windows-ocr"),
    ("winrt.windows.media.ocr", "windows-ocr"),
    ("winsdk.windows.media.ocr", "windows-ocr"),
    ("paddleocr", "paddleocr"),
)

_PROBE: tuple[list[OcrEngine], list[tuple[str, bool, str]]] | None = None
# 已构造好的引擎复用表: 预热 (启动时后台加载模型) 和真正探测共用同一份实例, 不重复加载模型。
# key 用给人看的名字, 方便日志对上。
_ENGINE_CACHE: dict[str, OcrEngine] = {}
_ENGINE_LOCK = __import__("threading").Lock()


def _cached(factory) -> OcrEngine:
    """构造并复用引擎实例。

    加锁的原因: 启动时后台线程在预热 RapidOCR (加载 ONNX 要 1 秒多), 而主线程此时可能
    正好来探测后端 —— 不加锁就会两边各加载一遍模型 (慢一倍), 还可能写坏缓存。
    """
    key = "rapidocr" if factory is RapidOcrEngine else "windows-ocr"
    with _ENGINE_LOCK:
        if key not in _ENGINE_CACHE:
            _ENGINE_CACHE[key] = factory()
        return _ENGINE_CACHE[key]


def preload_heavy() -> None:
    """后台把**重模型** (RapidOCR 的 ONNX) 先加载好。

    只碰 RapidOCR: 它什么时候构造都安全。Windows OCR 必须等 QApplication 建好
    (见 `_winrt_safe_now`), 所以**不能**在这里一起做 —— 预热线程和 Qt 建窗口同时碰 winrt,
    实测同样段错误。
    """
    try:
        _cached(RapidOcrEngine)
        log.info("OCR 重模型已预热 (rapidocr)")
    except Exception as exc:                         # noqa: BLE001 - 预热失败不影响启动
        log.info("RapidOCR 预热跳过: %s", exc)


def _winrt_allowed(allow: bool | None) -> tuple[bool, str]:
    """现在构造 Windows OCR (winrt) 引擎安不安全; 返回 (能不能, 不能的原因)。

    实测 (Python 3.14 + winrt 3.2.1 + PySide6, 用户机器上稳定复现):
      * 先初始化 winrt, 之后在同一个进程里建 QApplication → **整个进程段错误**
        (无 traceback; pythonw 下连报错都没有, 表现就是"双击没反应")
      * 反过来 (先 QApplication, 再在主线程构造 winrt) → 完全正常

    所以只要这个进程**有可能**要用 Qt, 就必须等 QApplication 建好之后再碰 winrt。
    注意"还没 import PySide6"不等于安全: 本应用正是先探测后端、再建 QApplication 的
    (实测就是在这一步段错误: 预探测里构造了 winrt, 紧接着建 QApplication 就崩)。
    """
    if allow is not None:
        return (True, "") if allow else (False, "调用方明确要求暂缓")
    if "PySide6.QtWidgets" in sys.modules or "PySide6.QtCore" in sys.modules:
        try:
            from PySide6.QtWidgets import QApplication

            if QApplication.instance() is not None:
                return True, ""
        except Exception:                            # noqa: BLE001
            pass
        return False, "要等 QApplication 建好 (先 winrt 后 Qt 会段错误)"
    import importlib.util

    if importlib.util.find_spec("PySide6") is None:
        return True, ""                              # 这台机器没装 Qt, 纯命令行场景
    return False, "要等界面就绪 (装了 PySide6 就随时可能建 QApplication)"


def probe_engines(force: bool = False, allow_winrt: bool | None = None
                  ) -> tuple[list[OcrEngine], list[tuple[str, bool, str]]]:
    """真的构造一遍每个后端, 返回 (可用引擎, 逐项体检结果)。

    **不能只查"包在不在"**: 包装了但加载失败 (缺 onnxruntime/DLL、模型文件缺失、版本冲突)
    时, 按包名判断会说"有 OCR", 而用户按下 Alt+Q 之后才收到"未安装任何 OCR 后端" ——
    这正是客户反馈里"系统提示没装 OCR, 但识别窗口还是弹出来了"的成因。

    顺序**不能反**: 实测先跑 Windows OCR 的异步调用、再跑 RapidOCR 的 onnxruntime 推理,
    整个进程会静默死掉 (没有异常、没有退出码), 反过来两个都正常。所以固定"rapidocr 优先"。

    allow_winrt: 只有"这个进程确定不会建 Qt"(命令行工具) 才传 True; 界面程序走默认值 ——
    它会在 QApplication 就绪前自动暂缓 winrt, 等界面起来后再重探。
    """
    global _PROBE
    if _PROBE is not None and not force:
        return _PROBE
    engines: list[OcrEngine] = []
    notes: list[tuple[str, bool, str]] = []
    deferred = False
    for factory in (RapidOcrEngine, WindowsOcrEngine):
        if factory is WindowsOcrEngine:
            ok, reason = _winrt_allowed(allow_winrt)
            if not ok:
                notes.append((factory.__name__, False, f"暂缓: {reason}"))
                deferred = True
                continue
        try:
            eng = _cached(factory)
        except Exception as exc:                 # noqa: BLE001 - 逐个后端降级, 不能整体失败
            notes.append((factory.__name__, False, f"{type(exc).__name__}: {exc}"))
            log.info("OCR 后端 %s 不可用: %s", factory.__name__, exc)
            continue
        engines.append(eng)
        notes.append((factory.__name__, True, eng.name))
    result = (engines or [StubOcrEngine()], notes)
    # 结论不完整 (有后端被暂缓) 时**不缓存** —— 否则界面起来后会一直拿到"只有 Stub"的旧结论,
    # 明明能用 Windows 自带 OCR 却被判成"这台机器没装 OCR"。
    if not deferred:
        _PROBE = result
    return result


def diagnosis() -> str:
    """一屏说清"这台机器上 OCR 到底行不行、不行是为什么、怎么修"。

    给两处用: 命令行 `--ocr-check` (用户/我们排障), 以及界面上"圈选用不了"时的提示里那句原因。
    """
    _engines, notes = probe_engines()
    lines = ["OCR 后端体检:"]
    for name, ok, detail in notes:
        lines.append(f"  [{'可用' if ok else '不可用'}] {name}: {detail}")
    if not any(ok for _n, ok, _d in notes):
        lines.append("  可用的包（装一个就能用圈选识别）:")
        lines.append("    pip install rapidocr-onnxruntime     # 离线首选(约 100MB, 印刷体效果最好)")
        lines.append("    pip install mss                      # 截屏 (没有它冻结层抓不到图)")
        lines.append("    pip install winrt-runtime winrt-Windows.Media.Ocr "
                     "winrt-Windows.Graphics.Imaging   # 用 Windows 自带的 OCR(小)")
        lines.append("    或 pip install -r requirements.txt")
    return "\n".join(lines)


def available_backends() -> list[str]:
    """这台机器上**装了哪些** OCR 包 (只看包在不在, 不加载模型)。

    只作为"要不要去构造引擎"的廉价线索 —— 真正判断能不能识别必须用 probe_engines()。
    """
    import importlib.util

    found = []
    for module, label in _BACKENDS:
        try:
            if importlib.util.find_spec(module) is not None and label not in found:
                found.append(label)
        except (ImportError, ValueError):            # 包损坏/命名空间异常都当没有
            continue
    return found


# ---------------------------------------------------------------- 预处理与融合

def preprocess(png: bytes, *, scale: int = 2) -> bytes:
    """小字/细笔画放大 + 灰度 + 自动对比度, 明显提升截图类小字号识别率。

    必须整体容错: 截图链路偶发拿到异常帧 (屏幕关闭、DRM 黑屏、区域越界) 时,
    宁可让 OCR 去试原图, 也不能把异常抛回热键回调, 否则 Alt+Q 会直接没反应。
    """
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(png)).convert("L")
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        img = ImageOps.autocontrast(img, cutoff=1)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        log.debug("图像预处理失败, 退回原图", exc_info=True)
        return png


def fuse(texts: Iterable[str]) -> str:
    """多引擎融合: 同长度时按字符多数投票, 否则取最长的结果。"""
    cands = [t.strip() for t in texts if t and t.strip()]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    longest = max(cands, key=len)
    if all(len(c) == len(longest) for c in cands):
        out = []
        for chars in zip(*cands):
            out.append(max(set(chars), key=chars.count))
        return "".join(out)
    sims = [difflib.SequenceMatcher(None, c, longest).ratio() for c in cands]
    return longest if max(sims) > 0.6 else cands[0]


# ---------------------------------------------------------------- 主流程

class Scanner:
    def __init__(self, engines: list[OcrEngine] | None = None,
                 capturer: Callable[[tuple[int, int, int, int]], bytes] | None = None,
                 clock=None, fuzzy_threshold: float = 0.55):
        self.engines = engines if engines is not None else default_engines()
        self.capturer = capturer or grab_screen_png
        self.clock = clock
        self.fuzzy_threshold = fuzzy_threshold

    def scan(self, sel: Selection, *, now=None) -> ScanResult:
        raw = self.capturer(sel.bbox)
        text, conf, used = self._ocr(raw)
        moment = now or (self.clock.now() if self.clock else None)
        # 逐行挑句: OCR 认到的是整屏文字, 直接整段解析会把聊天时间戳/时刻表也算成日程。
        picked = pick_line(text, now=moment)
        return ScanResult(text=text, parsed=picked.parsed, bbox=sel.bbox,
                          engine="+".join(used), confidence=conf,
                          focus=picked.line, rejected=picked.rejected)

    def scan_against(self, sel: Selection, active: list[Reminder], *, now=None) -> ScanResult:
        res = self.scan(sel, now=now)
        res.matched_reminders = match_reminders(res.text, active, self.fuzzy_threshold)
        return res

    def _ocr(self, png: bytes) -> tuple[str, float, list[str]]:
        if isinstance(self.engines[0], StubOcrEngine):
            raise RuntimeError(self.engines[0].recognize(b""))
        prepped = preprocess(png)
        texts, used = [], []
        for eng in self.engines:
            try:
                texts.append(eng.recognize(prepped))
                used.append(eng.name)
            except Exception as exc:
                log.warning("OCR 引擎 %s 失败: %s", eng.name, exc)
        merged = fuse(texts)
        conf = 0.0
        if merged:
            conf = max(difflib.SequenceMatcher(None, t, merged).ratio() for t in texts if t)
        return merged, round(conf, 3), used


def match_reminders(text: str, active: list[Reminder], threshold: float = 0.55) -> list[Reminder]:
    """圈划删除: 圈到相同/相近内容即认为用户想删除那条提醒。"""
    t = _norm(text)
    if not t:
        return []
    hits = []
    for rem in active:
        for cand in (rem.title, rem.source_text):
            if not cand:
                continue
            if _norm(cand) in t or t in _norm(cand):
                hits.append(rem)
                break
            if difflib.SequenceMatcher(None, _norm(cand), t).ratio() >= threshold:
                hits.append(rem)
                break
    return hits


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if not ch.isspace())


# ---------------------------------------------------------------- 截图

def grab_screen_png(bbox: tuple[int, int, int, int]) -> bytes:
    """截取屏幕区域为 PNG 字节。优先 mss, 退化为 Qt 截屏。"""
    try:
        import mss
        from PIL import Image

        x0, y0, x1, y1 = bbox
        # mss 1.0 之后 mss.mss() 改名成 MSS (旧名字会打 DeprecationWarning), 两个都认
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            shot = sct.grab({"left": x0, "top": y0, "width": max(1, x1 - x0),
                             "height": max(1, y1 - y0)})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        log.debug("mss 不可用, 尝试 Qt 截屏", exc_info=True)

    from PySide6.QtGui import QGuiApplication  # pragma: no cover - 需 GUI

    x0, y0, x1, y1 = bbox
    screen = QGuiApplication.primaryScreen()
    pix = screen.grabWindow(0, x0, y0, x1 - x0, y1 - y0)
    buf = io.BytesIO()
    pix.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 验收量化

def _flatten(text: str) -> str:
    """比对前把空白(含 OCR 的换行/空格)抹平。

    实测教训: 多行截图里 OCR 会按行返回带 \n 的文本, 而人工标注通常不带换行 ——
    逐字 zip 比对时每个换行都会错一位, 后面整句跟着错位, 同一张几乎全对的图能被算成 13%。
    空白差异不是识别错误, 不该计入准确率; 标点差异仍然计入。
    """
    return "".join(ch for ch in text if not ch.isspace())


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离 (纯 Python, 一行 DP)。只用于评估, 不在识别路径上。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _char_accuracy(got: str, want: str) -> tuple[int, int]:
    """返回 (命中字符数, 总字符数), 命中 = 总长 - 编辑距离。

    为什么不用"逐位相等": OCR 少认一个字符, 后面的字符全部错位, 逐位比对会把
    一张 141 字里只错 5 个字的图算成 12%。PRD 4.2 要的是"准确率"这个数,
    标准做法是 1 - 编辑距离/长度 (字符错误率 CER 的补数)。
    """
    if not want:
        return 0, 0
    distance = _edit_distance(got, want)
    return max(0, len(want) - distance), len(want)


def _same_minute(a, b) -> bool:
    """两个时刻是否落在同一分钟 (时区已归一; 秒与微秒不参与比较)。"""
    try:
        left = a.replace(second=0, microsecond=0)
        right = b.replace(second=0, microsecond=0)
        return left == right
    except (AttributeError, TypeError):
        return False


def evaluate(engine: OcrEngine, samples: list[tuple[bytes, str]]) -> dict:
    """PRD 4.2 的"识别准确率需满足实际使用需求"必须可度量:
    给一批 (截图, 人工标注文本) 计算 字符准确率 与 时间抽取准确率。

    字符准确率按"抹平空白后逐位相等"统计; 时间抽取准确率按"这段文本里有人工标注的时间,
    识别结果里也解析出了时间"统计。
    """
    char_total = char_hit = time_total = time_hit = 0
    value_total = value_hit = 0
    for png, truth in samples:
        got = _flatten(engine.recognize(preprocess(png)))
        want = _flatten(truth)
        hit, total = _char_accuracy(got, want)
        char_hit += hit
        char_total += total
        want_time = parse_time(truth)
        if want_time and want_time.when:
            time_total += 1
            got_time = parse_time(got)
            time_hit += int(bool(got_time and got_time.when))
            # "提取到了时间" 不等于 "提取到了**对的**时间"(下午认成上午也是提取到了)。
            # 这里再比一层: 解析出的时刻要和人写的一致(精确到分钟)。
            value_total += 1
            if got_time and got_time.when:
                value_hit += int(_same_minute(got_time.when, want_time.when))
    return {
        "char_accuracy": round(char_hit / char_total, 4) if char_total else 0.0,
        "time_extract_accuracy": round(time_hit / time_total, 4) if time_total else 0.0,
        "time_value_accuracy": round(value_hit / value_total, 4) if value_total else 0.0,
        "samples": len(samples),
    }
