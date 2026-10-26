"""OCR 评估口径用例。

客户 PRD 4.2 的准确率要靠 `ocr.evaluate()` 出数字，所以这个函数本身的口径必须先钉住 ——
实测踩过的坑：OCR 按行返回带换行的文本，人工标注不带换行，逐位比对时每个换行都错一位，
一张几乎全对的图能被算成 13%。空白差异不是识别错误。
"""

from __future__ import annotations

from reminder_assistant import ocr


class FakeEngine:
    """把固定文本当成识别结果返回，用来只验口径、不验引擎。"""

    name = "fake"

    def __init__(self, text: str):
        self.text = text

    def recognize(self, png: bytes, *, lang: str = "chi_sim+eng") -> str:
        return self.text


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32          # preprocess() 只要求是字节流


def test_newlines_do_not_count_as_errors():
    engine = FakeEngine("明天下午三点开会\n交周报")
    result = ocr.evaluate(engine, [(PNG, "明天下午三点开会交周报")])
    assert result["char_accuracy"] == 1.0
    assert result["samples"] == 1


def test_spaces_do_not_count_as_errors():
    engine = FakeEngine("交 周 报")
    result = ocr.evaluate(engine, [(PNG, "交周报")])
    assert result["char_accuracy"] == 1.0


def test_real_misrecognition_still_counted():
    engine = FakeEngine("明天下午二点开会")
    result = ocr.evaluate(engine, [(PNG, "明天下午三点开会")])
    assert 0.0 < result["char_accuracy"] < 1.0


def test_one_missing_char_costs_one_char_not_everything():
    """少认一个字符只该扣一个字的分 —— 逐位比对会把它算成"后面全错"。"""
    engine = FakeEngine("明天下午三点开")
    result = ocr.evaluate(engine, [(PNG, "明天下午三点开会")])
    assert result["char_accuracy"] >= 0.85


def test_one_extra_char_does_not_wreck_the_score():
    engine = FakeEngine("明天下午三点开会了")
    result = ocr.evaluate(engine, [(PNG, "明天下午三点开会")])
    assert result["char_accuracy"] >= 0.85


def test_punctuation_difference_is_not_forgiven():
    # 逗号认成句号是真实错误，不能和空白一起被抹掉
    engine = FakeEngine("开会。")
    result = ocr.evaluate(engine, [(PNG, "开会，")])
    assert result["char_accuracy"] < 1.0


def test_time_extraction_accuracy():
    engine = FakeEngine("开会提醒：明天下午三点")
    result = ocr.evaluate(engine, [(PNG, "明天下午三点开会")])
    assert result["time_extract_accuracy"] == 1.0

    engine_bad = FakeEngine("开会提醒")
    result_bad = ocr.evaluate(engine_bad, [(PNG, "明天下午三点开会")])
    assert result_bad["time_extract_accuracy"] == 0.0


def test_empty_samples_is_zero_not_crash():
    result = ocr.evaluate(FakeEngine("x"), [])
    assert result == {"char_accuracy": 0.0, "time_extract_accuracy": 0.0, "samples": 0}
