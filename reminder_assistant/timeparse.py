"""中文自然语言时间解析 (纯规则, 零依赖, 可离线)。

覆盖 PRD 明确要求的表达:
    "明天下午三点"  "下周一上午10点"  "5分钟后"  "半小时后"
    "3月5日 15:30"  "明天早上"  "下周三"  "晚上8点"  "3:30pm"
以及提醒规则里的 "提前一小时" / "间隔20分钟"。

设计取舍:
  * 最小区间锚定 —— 统一用带时区的 datetime, 不做 naive 运算 (夏令时/跨时区安全)。
  * 歧义不隐藏 —— 例如无上下午标记的 "三点" 返回主选 + alternatives + confidence,
    UI 必须把 alternatives 展示成可点选的候选, 而不是默默猜一个。
  * "现在" 由外部注入 —— 解析器不读系统时间, 保证可测、可回放。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

# ---------------------------------------------------------------- 数字

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
NUM = r"(?:\d{1,4}|[零〇一二三四五六七八九十百两]{1,4})"


def cn_to_int(s: str) -> int | None:
    """'十五'->15, '二十五'->25, '一百零五'->105, '23'->23。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    section, number = 0, 0
    seen = False
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            section += (number or 1) * unit
            number = 0
            seen = True
        else:
            return None
    return (section + number) if seen else None


# ---------------------------------------------------------------- 词表

REL_DAYS = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明儿": 1,
            "今天": 0, "今日": 0, "本日": 0, "昨天": -1, "前天": -2}

PERIOD_DEFAULT_HOUR = {"凌晨": 1, "清晨": 6, "早上": 8, "早晨": 8, "上午": 9,
                       "中午": 12, "下午": 15, "傍晚": 18, "晚上": 20,
                       "夜里": 21, "早": 8, "晚": 20}
PM_PERIODS = {"中午", "下午", "傍晚", "晚上", "夜里", "晚"}
AM_PERIODS = {"凌晨", "清晨", "早上", "早晨", "上午", "早"}

DOW = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 6}
WEEK_OFFSET = {"上": -1, "这": 0, "本": 0, "下": 1, "下下": 2, "": None}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# ---------------------------------------------------------------- 正则

RE_FULL_DATE = re.compile(rf"(?P<y>\d{{2,4}})\s*年\s*(?P<mo>{NUM})\s*月\s*(?P<d>{NUM})\s*[日号]?")
RE_ISO = re.compile(r"(?P<y>\d{4})\s*[-/.·・。．]\s*(?P<mo>\d{1,2})\s*[-/.·・。．]\s*(?P<d>\d{1,2})")
RE_MD = re.compile(rf"(?P<mo>{NUM})\s*月\s*(?P<d>{NUM})\s*[日号]")
RE_SLASH = re.compile(r"(?<!\d)(?P<mo>\d{1,2})\s*/\s*(?P<d>\d{1,2})(?!\d)")
RE_REL_DAY = re.compile("|".join(sorted(REL_DAYS, key=len, reverse=True)))
RE_WEEKDAY = re.compile(r"(?:(?P<wk>下下|下|这|本|上)\s*)?(?:周|个?星期|礼拜)\s*(?P<dow>[一二三四五六日天末])")
RE_WEEK_ONLY = re.compile(r"(?P<wk>下下|下|这|本)\s*(?:个?周|个?星期|礼拜)(?![一二三四五六日天末])")
RE_PERIOD_COMBO = re.compile(r"(?P<dpre>今|明|后|大后)\s*(?P<per>早上|晚上|上午|下午|中午|夜里|早|晚)")
# 单独出现的"早/晚"误报率太高 (晚安/早退), 只在带今/明前缀时才认
RE_PERIOD = re.compile("|".join(sorted(
    ("凌晨", "清晨", "早晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "夜里"),
    key=len, reverse=True)))
RE_TIME = re.compile(
    rf"(?<![小个每钟半])(?P<h>{NUM})\s*(?P<sep>[点时]|[:：])\s*"
    rf"(?:(?P<m>一刻|三刻|半|整|{NUM})\s*分?)?\s*(?P<ampm>am|pm|AM|PM)?"
)
_U = r"秒|分钟|分|小时|钟头|天|周|个?星期|礼拜|个?月"
_U2 = r"分钟|分|小时|天|秒"
RE_REL_OFFSET = re.compile(
    rf"(?:(?:还有|再过)\s*(?P<pn>{NUM}|半)\s*(?:个)?\s*(?P<pu>{_U}))"
    rf"|(?:(?P<half>半)\s*(?:个)?\s*(?P<hu>小时|钟头)"
    rf"|(?P<n1>{NUM})\s*(?:个)?\s*(?P<u1>{_U})"
    rf"(?:\s*(?P<n2>{NUM})\s*(?:个)?\s*(?P<u2>{_U2}))?)"
    rf"\s*(?:之?后|以后|后)(?![悔])"
)
RE_LEAD = re.compile(rf"提前\s*(?P<n>{NUM})?\s*(?P<half>半)?\s*(?:个)?\s*(?P<u>秒|分钟|分|小时|钟头|天|周|个?月)")
RE_INTERVAL = re.compile(rf"间隔\s*(?P<n>{NUM})?\s*(?P<half>半)?\s*(?:个)?\s*(?P<u>秒|分钟|分|小时|钟头|天)")

UNIT_SECONDS = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "钟头": 3600,
                "天": 86400, "周": 604800, "星期": 604800, "礼拜": 604800, "月": 2592000}


# ---------------------------------------------------------------- 结果

@dataclass(frozen=True)
class ParseResult:
    when: datetime | None
    matched: str = ""
    span: tuple[int, int] | None = None
    kind: str = "none"          # absolute|relative|date-only|period|lead|interval|none
    confidence: float = 0.0
    alternatives: tuple[datetime, ...] = ()
    lead: timedelta | None = None      # "提前一小时"
    interval: timedelta | None = None  # "间隔20分钟"
    notes: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.when is not None


@dataclass
class _Fields:
    year: int | None = None
    month: int | None = None
    day: int | None = None
    day_delta: int | None = None
    weekday: int | None = None
    week_offset: int | None = None
    hour: int | None = None
    minute: int | None = None
    period: str | None = None
    ampm: str | None = None
    clock_24: bool = False      # 形如 "09:00" 的前导零, 说明本来就是 24 小时制, 无歧义
    rel_delta: timedelta | None = None
    lead: timedelta | None = None
    interval: timedelta | None = None
    spans: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, tag)
    notes: list[str] = field(default_factory=list)

    @property
    def has_date(self) -> bool:
        return any(v is not None for v in
                   (self.year, self.month, self.day, self.day_delta, self.weekday, self.week_offset))

    @property
    def has_time(self) -> bool:
        return self.hour is not None or self.period is not None


# ---------------------------------------------------------------- 归一化

_FULLWIDTH = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９：／"}


def normalize(text: str) -> str:
    """全角->半角。长度保持不变, 因此 span 可直接映射回原文。"""
    return text.translate(_FULLWIDTH)


def _to_int(s: str | None) -> int | None:
    return cn_to_int(s) if s else None


def _norm_ymd(y: int | None, mo: int, d: int, now: datetime, notes: list[str]) -> date:
    if y is None:
        y = now.year
        try:
            cand = date(y, mo, d)
        except ValueError:
            notes.append(f"非法日期 {mo}月{d}日")
            return now.date()
        if cand < now.date():
            y += 1
            notes.append(f"未写年份且已过, 顺延至 {y} 年")
    try:
        return date(y if y > 99 else 2000 + y, mo, d)
    except ValueError:
        notes.append(f"非法日期 {mo}月{d}日")
        return now.date()


def _apply_period(hour: int, period: str | None, notes: list[str]) -> tuple[int, int]:
    """返回 (小时, 天偏移)。"""
    if period is None:
        return hour, 0
    if period in PM_PERIODS:
        if hour == 12:
            if period in ("晚上", "夜里"):
                return 0, 1  # 晚上12点 == 次日 00:00
            return 12, 0
        if hour < 12:
            return hour + 12, 0
        return hour, 0
    if period in AM_PERIODS:
        if hour == 12:
            return 0, 0  # 凌晨12点 == 00:00
        return hour, 0
    return hour, 0


def _safe_local(d: date, hh: int, mm: int, tz) -> datetime:
    """构造本地时间。夏令时"春季跳变"会产生不存在的时刻 (如 NY 3 月某日 02:30):
    此时 zoneinfo 会给出 fold=0 的幻影偏移, 用 round-trip 检测并顺延到真实时刻,
    避免提醒落在 1 小时之外。"""
    dt = datetime.combine(d, time(hh, mm), tzinfo=tz)
    rt = dt.astimezone(timezone.utc).astimezone(tz)
    if (rt.hour, rt.minute) != (hh, mm):
        dt = rt
    return dt


# 没写上午/下午时, 凌晨 0~5 点这一档几乎不会被说成"明天3点" —— 先排除, 剩下的就是可用候选。
NIGHT_HOURS = frozenset(range(0, 6))


def _nearest_of_two(day: date, hour: int, minute: int, now: datetime, tz, notes: list[str],
                    alts: list[datetime]) -> tuple[datetime, list[datetime]]:
    """只有一个小数字小时、又没写上午/下午时 (`明天8点`), 在上午/下午两个候选里**取离现在最近的**。

    以前这里写死 `hour += 12` (默认下午), 于是 "明天8点吃饭" 被定成 20:00 —— 客户反馈原话
    "没有明确上下午机器会出现错觉, 默认是下午"。就近取值既符合"下一次那个 8 点"的直觉, 又和
    "没有日期的裸时刻取最近将来"是同一套口径, 不用给用户解释两套规则。

    凌晨候选 (0~5 点) 先排除; 排除后只剩下的那个就是答案。两个候选都已经过去时 (例如上午说
    "今天8点") 取**较晚**的那个 —— 提醒落在将来才是有用的。
    """
    cands = [_safe_local(day, hour, minute, tz), _safe_local(day, hour + 12, minute, tz)]
    allowed = [c for c in cands if c.hour not in NIGHT_HOURS] or cands
    future = [c for c in allowed if c > now]
    when = min(future, key=lambda c: c - now) if future else max(allowed)
    others = [c for c in cands if c != when]
    notes.append(f"未写明上午/下午, 按月就近取值 {when:%m-%d %H:%M}"
                 + (f" (备选 {others[0]:%m-%d %H:%M})" if others else ""))
    return when, alts + others


# ---------------------------------------------------------------- 主入口

def parse_time(text: str, now: datetime | None = None, tz=None) -> ParseResult:
    """从任意文本里抽出时间语义。now 必须带时区 (缺省用本机时区)。"""
    from .clock import local_tz

    if now is None:
        tz = tz or local_tz()
        now = datetime.now(tz)
    else:
        tz = tz or now.tzinfo or local_tz()
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)

    raw = text or ""
    s = normalize(raw)
    f = _Fields()

    # 1) 相对偏移优先级最高 ("5分钟后" 比同句里的 "明天" 更具体)
    m = RE_REL_OFFSET.search(s)
    if m and (m.group("pn") or m.group("n1") or m.group("half")):
        f.rel_delta = _rel_delta(m)
        f.spans.append((*m.span(), "rel"))

    if f.rel_delta is None:
        _scan_date(s, f, now)
        _scan_time(s, f)

    m_lead = RE_LEAD.search(s)
    if m_lead:
        f.lead = _unit_delta(m_lead.group("n"), m_lead.group("half"), m_lead.group("u"))
        f.spans.append((*m_lead.span(), "lead"))
    m_int = RE_INTERVAL.search(s)
    if m_int:
        f.interval = _unit_delta(m_int.group("n"), m_int.group("half"), m_int.group("u"))
        f.spans.append((*m_int.span(), "interval"))

    if not f.spans:
        return ParseResult(when=None, kind="none", matched="",
                           lead=f.lead, interval=f.interval,
                           notes=("未识别到时间表达",))

    span = _choose_span(s, f.spans)
    start, end = span
    matched = raw[start:end]

    if f.rel_delta is not None:
        when = now + f.rel_delta
        return ParseResult(when=when, matched=matched, span=span, kind="relative",
                           confidence=0.95, lead=f.lead, interval=f.interval,
                           notes=tuple(f.notes))

    when, alts, conf = _resolve(f, now, tz)
    if when is None:
        return ParseResult(when=None, matched=matched, span=span, kind="none",
                           lead=f.lead, interval=f.interval, notes=tuple(f.notes))
    kind = "date-only" if (f.has_date and not f.has_time) else "absolute"
    if f.has_time and not f.has_date:
        kind = "period" if f.hour is None else "absolute"
    return ParseResult(when=when, matched=matched, span=(start, end), kind=kind,
                       confidence=conf, alternatives=tuple(alts),
                       lead=f.lead, interval=f.interval, notes=tuple(f.notes))


def parse_all(text: str, now: datetime | None = None, tz=None) -> list[ParseResult]:
    """一段文本里的多个时间表达 (OCR 一段可能含多条)。"""
    from .clock import local_tz

    if now is None:
        now = datetime.now(tz or local_tz())
    results: list[ParseResult] = []
    work, offset = text, 0
    for _ in range(20):
        r = parse_time(work, now=now, tz=tz)
        if not r or r.span is None:
            break
        a, b = r.span
        results.append(ParseResult(
            when=r.when, matched=r.matched, span=(a + offset, b + offset), kind=r.kind,
            confidence=r.confidence, alternatives=r.alternatives, lead=r.lead,
            interval=r.interval, notes=r.notes))
        # 已消费区间用等长空格掩掉, 保证后续 span 仍可映射回原文
        work = work[:a] + " " * (b - a) + work[b:]
        offset = offset  # work 与原文等长, 无需累加
    return results


# ---------------------------------------------------------------- 子扫描

# 这些字符出现在两个候选区间之间, 说明它们属于不同小句, 不能合并成一个时间表达
_GAP_BREAK = set(",，。;；、!！?？/\\|()（）[]【】<>《》\"'“”‘’ \t\r\n0123456789")


def _choose_span(text: str, spans: list[tuple[int, int, str]]) -> tuple[int, int]:
    """把相邻的日期/时段/时刻片段合成一个"时间表达"区间。

    必须做邻近性聚类: "明天开会, 5分钟后先打电话" 里的 "明天" 和 "5分钟后"
    是两个独立表达, 如果直接取 min..max 会把整句圈进去, 卡片标题就错了。
    """
    ordered = sorted(spans)
    clusters: list[list[tuple[int, int, str]]] = [[ordered[0]]]
    for sp in ordered[1:]:
        prev = clusters[-1][-1]
        gap = text[prev[1]:sp[0]]
        if sp[0] - prev[1] <= 3 and not any(ch in _GAP_BREAK for ch in gap):
            clusters[-1].append(sp)
        else:
            clusters.append([sp])

    def priority(c: list[tuple[int, int, str]]) -> int:
        tags = {t for *_, t in c}
        for i, tag in enumerate(("rel", "time", "period", "date")):
            if tag in tags:
                return i
        return 9  # 只剩 lead/interval 时退回第一个簇

    best = min(clusters, key=priority)
    return min(a for a, _, _ in best), max(b for _, b, _ in best)

def _scan_date(s: str, f: _Fields, now: datetime) -> None:
    for rx in (RE_FULL_DATE, RE_ISO, RE_MD, RE_SLASH):
        m = rx.search(s)
        if m:
            f.year = _to_int(m.group("y")) if "y" in m.groupdict() else None
            f.month = _to_int(m.group("mo"))
            f.day = _to_int(m.group("d"))
            f.spans.append((*m.span(), "date"))
            break

    if f.month is None:
        m = RE_REL_DAY.search(s)
        if m:
            f.day_delta = REL_DAYS[m.group(0)]
            f.spans.append((*m.span(), "date"))

    m = RE_WEEKDAY.search(s)
    if m:
        f.weekday = DOW[m.group("dow")]
        f.week_offset = WEEK_OFFSET.get(m.group("wk") or "", None)
        f.spans.append((*m.span(), "date"))
    elif (m := RE_WEEK_ONLY.search(s)):
        f.week_offset = WEEK_OFFSET.get(m.group("wk"), 1)
        f.notes.append("只有周次没有星期几, 按同日顺延计算")
        f.spans.append((*m.span(), "date"))

    m = RE_PERIOD_COMBO.search(s)
    if m:
        if f.day_delta is None:
            f.day_delta = REL_DAYS.get(m.group("dpre") + "天", 1 if m.group("dpre") in "明后" else 0)
        f.period = m.group("per")
        f.spans.append((*m.span(), "period"))
    else:
        m = RE_PERIOD.search(s)
        if m:
            f.period = m.group(0)
            f.spans.append((*m.span(), "period"))


def _scan_time(s: str, f: _Fields) -> None:
    m = RE_TIME.search(s)
    if not m:
        return
    h = _to_int(m.group("h"))
    if h is None or h > 24:
        return
    ms = m.group("m")
    if ms is None:
        minute = 0
    elif ms in ("半",):
        minute = 30
    elif ms == "一刻":
        minute = 15
    elif ms == "三刻":
        minute = 45
    elif ms == "整":
        minute = 0
    else:
        minute = _to_int(ms)
        if minute is None or minute > 59:
            return
    if h == 24:
        h, minute = 0, 0
    f.hour, f.minute = h, minute
    hraw = m.group("h")
    f.clock_24 = (m.group("sep") == ":" and hraw.isdigit()
                  and len(hraw) >= 2 and hraw[0] == "0")
    if m.group("ampm"):
        f.ampm = m.group("ampm").lower()
    f.spans.append((*m.span(), "time"))


def _rel_delta(m: re.Match) -> timedelta | None:
    if m.group("pu"):                       # "还有10分钟" / "再过一小时"
        return _unit_delta(m.group("pn"), "半" if m.group("pn") == "半" else None, m.group("pu"))
    if m.group("half"):                     # "半小时后"
        return timedelta(seconds=0.5 * UNIT_SECONDS[m.group("hu")])
    total = _unit_delta(m.group("n1"), None, m.group("u1"))
    if m.group("n2"):
        total = (total or timedelta(0)) + _unit_delta(m.group("n2"), None, m.group("u2"))
    return total


def _unit_delta(n: str | None, half: str | None, unit: str | None) -> timedelta | None:
    if unit is None:
        return None
    amount = 0.5 if half else float(_to_int(n) or 1)
    return timedelta(seconds=amount * UNIT_SECONDS.get(unit, 0))


# ---------------------------------------------------------------- 归一求解

def _resolve(f: _Fields, now: datetime, tz) -> tuple[datetime | None, list[datetime], float]:
    notes = f.notes
    alts: list[datetime] = []
    conf = 1.0
    day_shift = 0

    # ---- 日期部分
    if f.month is not None and f.day is not None:
        base_date = _norm_ymd(f.year, f.month, f.day, now, notes)
    elif f.weekday is not None:
        monday = now.date() - timedelta(days=now.weekday())
        base_date = monday + timedelta(days=f.weekday + 7 * (f.week_offset or 0))
        if f.week_offset is None and base_date < now.date():
            base_date += timedelta(days=7)
            notes.append("该星期几本周已过, 顺延到下周")
    elif f.week_offset is not None:
        base_date = now.date() + timedelta(days=7 * f.week_offset)
    elif f.day_delta is not None:
        base_date = now.date() + timedelta(days=f.day_delta)
    else:
        base_date = now.date()

    # ---- 时间部分
    hour, minute = f.hour, f.minute or 0
    if hour is None:
        if f.period is not None:
            hour = PERIOD_DEFAULT_HOUR[f.period]
            notes.append(f"只说“{f.period}”, 默认 {hour:02d}:00")
            conf = min(conf, 0.7)
        else:
            if f.has_date:
                hour = 9
                notes.append("只给了日期没给时间, 默认 09:00")
                conf = min(conf, 0.5)
            else:
                return None, [], 0.0

    if f.ampm == "pm" and hour < 12:
        hour += 12
    elif f.ampm == "am" and hour == 12:
        hour = 0

    if f.period is not None:
        hour, day_shift = _apply_period(hour, f.period, notes)
        if f.hour is not None and f.ampm is None:
            conf = min(conf, 0.9)

    # 有日期但没写上午/下午 ("明天三点") 同样有歧义。**不猜"默认下午"**, 而是在
    # 上午/下午两个候选里取离现在最近的那个 (实测客户反馈: "没有明确上下午机器会出现
    # 错觉, 默认是下午" —— "明天8点吃饭" 之前被定成 20:00)。
    # "09:00" 这种前导零写法本来就是 24 小时制, 不参与歧义推断。
    bare_ambiguous = (f.period is None and f.ampm is None and not f.clock_24
                      and f.hour is not None and 1 <= f.hour <= 11)
    if bare_ambiguous and f.has_date:
        when, alts = _nearest_of_two(base_date + timedelta(days=day_shift), int(f.hour or 0),
                                     minute, now, tz, notes, alts)
        conf = min(conf, 0.7)
    else:
        when = _safe_local(base_date + timedelta(days=day_shift), hour, minute, tz)

    # ---- 无日期的裸时刻: 最近未来优先 + 暴露歧义
    if bare_ambiguous and not f.has_date:
        today = now.date()
        cands = [datetime.combine(today, time(f.hour, minute), tzinfo=tz),
                 datetime.combine(today, time(f.hour + 12, minute), tzinfo=tz),
                 datetime.combine(today + timedelta(days=1), time(f.hour, minute), tzinfo=tz)]
        future = [c for c in cands if c > now]
        when = future[0] if future else cands[2]
        alts = [c for c in future[1:]]
        conf = min(conf, 0.7)
        notes.append("没写上午/下午, 按最近的将来时刻取值, 请确认")

    if not f.has_date and when <= now and f.day_delta is None:
        when += timedelta(days=1)
        notes.append("该时刻今天已过, 顺延到明天")
        conf = min(conf, 0.8)

    return when, alts, conf
