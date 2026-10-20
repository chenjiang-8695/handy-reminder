"""从一次圈选的 OCR 文本里挑出"该建卡的那一句"。

为什么需要这一层 (客户反馈: "ocr 识别内容抗干扰太弱容易识别不相干内容"):

实测用用户给的 4 张测试图跑 Windows 自带 OCR, 拿到的是**整屏**的文本:
  * 微信聊天截图: 圈选区域里除了气泡里的 "明天8点吃饭", 上面还有一条消息时间戳
    "10月8日14:17" —— 旧实现把整段文本丢给 parse_time, 取到的是那个时间戳
    (而且"10月8日"已过, 还被顺延到了明年)。
  * 课表截图: 整屏 "08:20 / 09:00 / 09:05 / …"; 12306 车次列表: "08:39 / 08:55 / ¥21起"。
    旧实现随手挑一个时刻就建卡。

做法: 按行拆 -> 逐行独立解析 -> 打分 -> 只认最高分那一行。

  * 一行里除时间表达之外还有汉字        -> 像"一句日程"      (+0.25)
  * 整行只由时间/数字/符号构成          -> 像截图时间戳/状态栏 (-0.5)
  * 车次号、票价、"第N周"、"20张/16分"  -> 像表格/列表痕迹     (-0.5)
  * 同一行里两个以上互不相干的时刻      -> 像时刻表            (-0.5)
  * 全部分数低于阈值                    -> **不建卡**, 交给界面说"这段里没读到像日程的话"

**整段里只有一行带时间时无条件用它**: 用户圈的就是那一行 (例如只圈气泡里的 "8:30"),
惩罚规则不该把"用户明确圈出来的唯一内容"也拒掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .timeparse import ParseResult, normalize, parse_all, parse_time

SELECT_MIN = 0.6          # 低于这个分数就不建卡
EVENT_BONUS = 0.25        # 除时间之外还有"事件词" -> 像一句日程
CONTENT_BONUS = 0.15      # 有别的汉字但没有事件词 -> 像一句话, 弱一点
NO_CONTENT_PENALTY = 0.5  # 整行只有时间 -> 像截图时间戳/状态栏
FRAGMENT_PENALTY = 0.6    # 时间 + 一两个零碎字, 且数字占了大半行 -> 像表格碎片
NOISE_PENALTY = 0.5       # 每命中一条版式噪声
DATE_ONLY_PENALTY = 0.6   # 只读到日期没读到时刻 (时间只能靠"默认 09:00"编出来)

# "像一句日程"的证据: 用户圈到的那句话里通常有这些动作/事件词。
# 它只用于**在多个候选之间排序**, 不是"必须有" —— 没有它时弱一档 (CONTENT_BONUS), 仍然能建卡。
_EVENT_WORDS: tuple[str, ...] = (
    "开会", "会议", "上课", "课", "考试", "面试", "吃饭", "饭", "聚餐", "约会", "见面", "见",
    "接", "送", "取", "拿", "交", "买", "购", "去", "到", "出发", "出差", "航班", "高铁", "火车",
    "飞机", "体检", "看病", "挂号", "报名", "缴费", "付款", "交费", "还款", "报销", "快递", "寄",
    "打电话", "电话", "联系", "汇报", "提交", "截止", "活动", "安排", "团建", "培训", "讲座",
    "比赛", "演出", "电影", "办", "签", "预约", "提醒", "记得", "别忘", "复习", "运动", "健身",
    "加班", "值班",
)

# 界面/版式痕迹。每条罚一次, 连着命中就快速掉到阈值以下。
_NOISE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"[GDCZKTY]\s?\d{2,5}", "像车次/航班号"),
    (r"[¥￥$]\s?\d", "像价格"),
    (r"第\s*\d+\s*[周学期]", "像周次/学期表头"),
    (r"\d+\s*(?:分钟|分|张|元|起)", "像时长/余票/价格"),
)

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_DIGITY = re.compile(r"[\d:：.．/／\-—]")
# 只有"周次/星期几"、没有具体时刻的表头碎片 (课表/日历截图里的 "本周" "非本周" "第6周" "周五")
_HEADER_LIKE = re.compile(
    r"^(?:下下|下|这|本|上|第\s*\d+)?\s*(?:个?周|个?星期|礼拜)[一二三四五六日天末]?$")


@dataclass(frozen=True)
class LinePick:
    """挑句结果。`line` 为空表示没挑到; `rejected` 非空表示"有候选但都不像日程"。"""

    line: str = ""
    index: int = -1
    parsed: ParseResult | None = None
    score: float = 0.0
    notes: tuple[str, ...] = ()
    rejected: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.parsed and self.parsed.when)


def lines_of(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _content_part(line: str, span: tuple[int, int] | None) -> str:
    """把时间表达那一段挖掉, 返回剩下的字符 (判断"除时间之外还有没有内容")。

    span 是在 **normalize 之后** 的串上算出来的; normalize 只做全角->半角、长度不变,
    所以可以直接用来切原串。
    """
    if not span:
        return line
    a, b = span
    return line[:a] + line[b:]


def score_line(line: str, parsed: ParseResult, *, now=None, tz=None) -> tuple[float, list[str]]:
    """给一行打分。返回 (分数, 命中的说明)。"""
    notes: list[str] = []
    score = float(parsed.confidence or 0.0)
    norm = normalize(line)
    content = _content_part(norm, parsed.span)
    cjk = _CJK.findall(content)

    if any(word in content for word in _EVENT_WORDS):
        score += EVENT_BONUS
    elif cjk:
        score += CONTENT_BONUS
    else:
        notes.append("整行只有时间")
        score -= NO_CONTENT_PENALTY

    # 表格碎片: 时间后面只跟一两个零碎字, 数字/时间符号占了大半行 —— 课表、时刻表里
    # 被换行切碎的单元格长这样 ("09：55大学")。真正的句子不会长成这样。
    if len(cjk) <= 2 and len(_DIGITY.findall(norm)) >= len(norm) / 2:
        notes.append("像被切碎的表格/时刻表")
        score -= FRAGMENT_PENALTY

    for pattern, why in _NOISE_PATTERNS:
        if re.search(pattern, norm):
            notes.append(why)
            score -= NOISE_PENALTY

    if len(parse_all(line, now=now, tz=tz)) >= 2:
        notes.append("一行里有两个以上时刻")
        score -= NOISE_PENALTY

    # 只有"周次/星期几"、旁边没有别的话 -> 课表/日历的表头 ("非本周" 会推出一个默认 09:00,
    # 分数看着不低, 但那不是一个日程)。
    if _HEADER_LIKE.match(normalize(parsed.matched or "").strip()) and len(cjk) <= 1:
        notes.append("像课表/日历表头")
        score -= FRAGMENT_PENALTY

    # 只读到日期、没读到具体时刻 -> 那个时刻是解析器**编出来的**默认值 (09:00), 不能靠它建卡。
    # 实测: 车票订单页的 "发车时间：2026.10.07星期三" 会因此被建成"下周三 09:00"的假提醒。
    if parsed.kind == "date-only":
        notes.append("只读到日期, 没读到具体时刻")
        score -= DATE_ONLY_PENALTY

    return round(score, 3), notes


def pick_line(text: str, *, now=None, tz=None) -> LinePick:
    """从整段识别文本里挑出该建卡的那一行 (挑不到就返回空 LinePick)。"""
    cands: list[LinePick] = []
    past: list[LinePick] = []
    for i, line in enumerate(lines_of(text)):
        if not line:
            continue
        parsed = parse_time(line, now=now, tz=tz)
        if not parsed.when:
            continue
        score, notes = score_line(line, parsed, now=now, tz=tz)
        item = LinePick(line=line, index=i, parsed=parsed, score=score, notes=tuple(notes))
        if _is_past(parsed.when, now):
            # 已经过去的时间不建卡: 提醒器建一张永远不会响的卡是纯噪声。
            # (实测: 车票订单页的 "发车时间：2026.10.07" 原本会变成一张 "下周三 09:00" 的假提醒。)
            past.append(item)
            continue
        cands.append(item)

    if not cands:
        return LinePick(notes=tuple(sorted({n for c in past for n in c.notes})),
                        rejected=_reject_reason(cands, past))

    best = max(cands, key=lambda c: c.score)
    if best.score >= SELECT_MIN:
        return best
    if len(cands) == 1 and not past:
        # 用户圈的就是这一行 —— 惩罚规则不该把唯一的内容也拒掉
        return LinePick(line=best.line, index=best.index, parsed=best.parsed, score=best.score,
                        notes=best.notes + ("整段里只有这一行带时间, 按它建卡",))

    return LinePick(notes=tuple(sorted({n for c in cands for n in c.notes})),
                    rejected=_reject_reason(cands, past))


def _is_past(moment: datetime, now: datetime | None) -> bool:
    """这句话说的是不是**已经过去**的日子 (今天不算)。

    `now` 为 None 时解析器本来就用系统时间, 这里跟着用同一个口径。
    """
    today = now.date() if now is not None else datetime.now(moment.tzinfo).date()
    return moment.date() < today


def _read_times(items: list[LinePick], cap: int = 3) -> str:
    """把识别到的片段列出来 —— 让用户看到"软件到底读到了什么"。"""
    seen: list[str] = []
    for it in items:
        matched = (it.parsed.matched or "").strip() if it.parsed else ""
        if matched and matched not in seen:
            seen.append(matched)
    if not seen:
        return ""
    return "、".join(seen[:cap]) + ("…" if len(seen) > cap else "")


def _reject_reason(cands: list[LinePick], past: list[LinePick]) -> str:
    """不建卡时给界面一句说得清的话: 读到了什么 + 为什么不算日程。"""
    parts: list[str] = []
    if cands:
        read = _read_times(cands)
        parts.append(f"{len(cands)} 行都像时间戳/时刻表" + (f"（读到 {read}）" if read else ""))
    if past:
        # 取"最像正经条目"的那一条 (按分数, 不按时间): 车票屏里 "发车时间" 比页面底部的
        # 广告日期更值得报给用户。
        sample = max(past, key=lambda c: c.score)
        shown = (sample.parsed.matched if sample.parsed.kind == "date-only"
                 else f"{sample.parsed.when:%m-%d %H:%M}")
        if not cands:
            return f"这条时间已经过去了（{shown}），不用提醒"
        parts.append(f"另有 {len(past)} 处已经过去的时间（{shown}）")
    if not parts:
        return ""
    return "这段里没读到像日程的话：" + "；".join(parts)
