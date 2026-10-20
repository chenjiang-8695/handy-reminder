"""时间解析用例。锚点固定为 2026-10-08(周四) 10:00 Asia/Shanghai, 保证可复现。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from reminder_assistant.timeparse import cn_to_int, parse_all, parse_time

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=SH)  # 周四


def p(text, now=NOW, tz=None):
    return parse_time(text, now=now, tz=tz)


# ---------------------------------------------------------------- 中文数字
@pytest.mark.parametrize("s,expected", [
    ("十", 10), ("十五", 15), ("二十", 20), ("二十五", 25),
    ("一百零五", 105), ("两", 2), ("3", 3), ("二十三", 23),
])
def test_cn_to_int(s, expected):
    assert cn_to_int(s) == expected


def test_cn_to_int_rejects_garbage():
    assert cn_to_int("开会") is None
    assert cn_to_int("") is None


# ---------------------------------------------------------------- 日期+时刻
@pytest.mark.parametrize("text,expected", [
    ("明天下午三点开会", datetime(2026, 10, 9, 15, 0, tzinfo=SH)),
    ("明天下午3点开会", datetime(2026, 10, 9, 15, 0, tzinfo=SH)),
    ("明天下午３点开会", datetime(2026, 10, 9, 15, 0, tzinfo=SH)),   # 全角
    ("今晚八点交周报", datetime(2026, 10, 8, 20, 0, tzinfo=SH)),
    ("今天晚上8点", datetime(2026, 10, 8, 20, 0, tzinfo=SH)),
    ("后天上午10点半面试", datetime(2026, 10, 10, 10, 30, tzinfo=SH)),
    ("大后天中午", datetime(2026, 10, 11, 12, 0, tzinfo=SH)),
    ("下周一上午10点交报告", datetime(2026, 10, 12, 10, 0, tzinfo=SH)),
    ("本周五 18:00 复盘", datetime(2026, 10, 9, 18, 0, tzinfo=SH)),
    ("周日晚上7点", datetime(2026, 10, 11, 19, 0, tzinfo=SH)),
    ("明天15:30回复邮件", datetime(2026, 10, 9, 15, 30, tzinfo=SH)),
    ("明天三点一刻", datetime(2026, 10, 9, 15, 15, tzinfo=SH)),
    ("明天15点整", datetime(2026, 10, 9, 15, 0, tzinfo=SH)),
    ("明天晚上12点", datetime(2026, 10, 10, 0, 0, tzinfo=SH)),      # 晚上12点=次日0点
    ("2026年12月1日09:00 体检", datetime(2026, 12, 1, 9, 0, tzinfo=SH)),
    ("2026-12-01 09:00 体检", datetime(2026, 12, 1, 9, 0, tzinfo=SH)),
    ("12月25日下午2点", datetime(2026, 12, 25, 14, 0, tzinfo=SH)),
    ("3月5日15:30", datetime(2027, 3, 5, 15, 30, tzinfo=SH)),        # 已过则顺延次年
])
def test_absolute(text, expected):
    r = p(text)
    assert r.when == expected, f"{text} -> {r.when} ({r.notes})"
    assert r.matched and r.matched in text.replace("３", "3") or r.matched in text


def test_weekday_rolls_forward_when_past():
    r = p("周三下午两点")  # 本周三已过(今天是周四)
    assert r.when == datetime(2026, 10, 14, 14, 0, tzinfo=SH)


def test_matched_span_covers_whole_expression():
    r = p("明天下午三点开会")
    assert r.matched == "明天下午三点"
    a, b = r.span
    assert "明天下午三点开会"[a:b] == "明天下午三点"


# ---------------------------------------------------------------- 相对时间
@pytest.mark.parametrize("text,delta", [
    ("5分钟后提醒我", timedelta(minutes=5)),
    ("半小时后", timedelta(minutes=30)),
    ("两个小时后", timedelta(hours=2)),
    ("还有10分钟", timedelta(minutes=10)),
    ("1小时15分钟后出发", timedelta(hours=1, minutes=15)),
    ("3天后", timedelta(days=3)),
    ("一周后", timedelta(weeks=1)),
])
def test_relative(text, delta):
    r = p(text)
    assert r.kind == "relative"
    assert r.when == NOW + delta, f"{text} -> {r.when}"


# ---------------------------------------------------------------- 歧义与兜底
def test_bare_hour_exposes_alternatives():
    r = p("三点见")
    assert r.when == datetime(2026, 10, 8, 15, 0, tzinfo=SH)   # 最近将来: 今天15:00
    assert datetime(2026, 10, 9, 3, 0, tzinfo=SH) in r.alternatives
    assert r.confidence < 1.0
    assert any("上午/下午" in n for n in r.notes)


def test_bare_hour_with_date_excludes_night_hours():
    """"明天三点" -> 下午 15:00: 凌晨 3 点这一档先排除 (改动"默认下午"之后行为不变)。"""
    r = p("明天三点")
    assert r.when == datetime(2026, 10, 9, 15, 0, tzinfo=SH)
    assert r.alternatives == (datetime(2026, 10, 9, 3, 0, tzinfo=SH),)
    assert p("明天15点").when == datetime(2026, 10, 9, 15, 0, tzinfo=SH)


def test_past_time_tomorrow_roll():
    r = p("早上八点")
    assert r.when == datetime(2026, 10, 9, 8, 0, tzinfo=SH)     # 今天8点已过


def test_date_only_defaults_to_9am():
    r = p("明天提醒我交材料")
    assert r.when == datetime(2026, 10, 9, 9, 0, tzinfo=SH)
    assert r.kind == "date-only" and r.confidence <= 0.5


def test_period_only():
    assert p("中午").when == datetime(2026, 10, 8, 12, 0, tzinfo=SH)
    assert p("下午").when == datetime(2026, 10, 8, 15, 0, tzinfo=SH)
    assert p("晚上的事").when == datetime(2026, 10, 8, 20, 0, tzinfo=SH)


def test_no_time_returns_none():
    assert not p("把这段代码重构一下")
    r = p("把这段代码重构一下")
    assert r.kind == "none" and r.when is None


# ---------------------------------------------------------------- 规则字段
def test_lead_and_interval_are_extracted():
    r = p("提前一小时提醒会议")
    assert r.lead == timedelta(hours=1)
    assert r.when is None            # 事件时刻由用户/另一段文本给出

    r2 = p("间隔20分钟重复")
    assert r2.interval == timedelta(minutes=20)

    r3 = p("会议在明天下午三点, 提前半小时提醒")
    assert r3.when == datetime(2026, 10, 9, 15, 0, tzinfo=SH)
    assert r3.lead == timedelta(minutes=30)


# ---------------------------------------------------------------- 时区/夏令时
def test_dst_spring_forward_nonexistent_local_time():
    now = datetime(2026, 3, 7, 12, 0, tzinfo=NY)
    r = parse_time("3月8日凌晨2点30分", now=now, tz=NY)
    assert r.when.utcoffset() == timedelta(hours=-4)      # 已进入 EDT
    assert (r.when.hour, r.when.minute) == (3, 30)        # 幻影 02:30 顺延为 03:30


def test_dst_fall_back_ambiguous_time_keeps_fold_zero():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=NY)
    r = parse_time("11月1日凌晨1点30分", now=now, tz=NY)
    assert r.when.hour == 1 and r.when.fold == 0


def test_cross_timezone_instants_match():
    r_sh = parse_time("明天下午三点", now=NOW, tz=SH)
    assert r_sh.when.astimezone(ZoneInfo("UTC")).hour == 7  # 15:00 CST == 07:00 UTC


# ---------------------------------------------------------------- 多表达
def test_parse_all_multiple_expressions():
    rs = parse_all("明天开会, 5分钟后先打电话, 晚上8点再确认", now=NOW)
    kinds = [r.kind for r in rs]
    assert "relative" in kinds and "absolute" in kinds
    assert len(rs) >= 2
    # span 必须仍指向原文
    for r in rs:
        assert r.matched in "明天开会, 5分钟后先打电话, 晚上8点再确认"
