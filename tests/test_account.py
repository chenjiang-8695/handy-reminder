"""账户与设备绑定 (PRD 2.4) —— 客户反馈第 2 条"绑定功能没有实现"。

这里钉的是**规则**, 不是界面长相:
  * 识别码稳定、随机、不做找回 → 每次读出来都必须一样;
  * 绑定码: 一次性、5 分钟有效、方向要对、连错要锁、签发要限频;
  * 设备令牌: 绑定后发放、重连可用、撤销后失效 (否则"撤销"只是删了条记录);
  * 两处入口 (设置面板 / 同步桥) 不能各存一份状态互相覆盖 —— 这是"手机上绑好了,
    电脑端设备列表里没有"的根因。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from reminder_assistant import account as A
from reminder_assistant.store import Store

NOW = datetime(2026, 10, 14, 10, 0).astimezone()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "a.db")
    yield s
    s.close()


@pytest.fixture()
def book(store):
    return A.AccountBook(store)


# ---------------------------------------------------------------- 识别码
def test_account_id_is_stable_and_random(book, store):
    first = book.account.account_id
    assert first.startswith("HR-") and len(first) == 12
    assert book.reload().account_id == first, "识别码必须稳定, 不能每次读都换一个"

    other = A.AccountBook(store)
    assert other.account.account_id == first, "同一个数据目录必须是同一个账户"

    ids = {A.random_account_id() for _ in range(50)}
    assert len(ids) == 50, "识别码是唯一凭据, 不能有可推算/可碰撞的规律"


def test_account_survives_reopen(tmp_path):
    s = Store(tmp_path / "a.db")
    first = A.load(s).account_id
    A.issue_code(A.load(s), NOW)
    s.close()
    s2 = Store(tmp_path / "a.db")
    assert A.load(s2).account_id == first
    s2.close()


def test_corrupt_account_record_is_recreated(store):
    store.set_meta(A.META_KEY, "{不是 json")
    acct = A.load(store)
    assert acct.account_id.startswith("HR-")
    assert A.load(store).account_id == acct.account_id      # 重建后落盘了


# ---------------------------------------------------------------- 绑定码规则
def test_code_normalisation_accepts_handwritten_lookalikes():
    """手抄常见的 0/O、1/I/L 混淆要能对上 —— 否则用户会一直得到"码不对"。"""
    assert A.format_code("ab3d7f9k") == "AB3D-7F9K"
    assert A.format_code("AB3D 7F9K") == "AB3D-7F9K"
    assert A.normalize_code("OI23-4567") == "01234567"       # O→0, I→1
    assert A.valid_code("AB3D-7F9K")
    assert not A.valid_code("短了") and not A.valid_code("")


def test_code_is_single_use(book):
    _acct, item = book.issue_code(NOW, A.ROLE_DESKTOP)
    _acct, dev = book.bind(item.code, NOW, name="手机", platform="手机")
    assert dev.name == "手机"
    with pytest.raises(A.AccountError) as ei:
        book.bind(item.code, NOW, name="第二台", platform="手机")
    assert "用过" in str(ei.value)      # 说清原因, 而不是笼统的"码不对"


def test_code_expires_after_ttl(store):
    acct = A.load(store)
    item = A.issue_code(acct, NOW)
    later = NOW + A.CODE_TTL + timedelta(seconds=1)
    with pytest.raises(A.AccountError) as ei:
        A.verify_code(acct, item.code, later)
    assert "过期" in str(ei.value)


def test_wrong_code_locks_the_account_after_repeated_attempts(store):
    acct = A.load(store)
    A.issue_code(acct, NOW)
    for i in range(A.LOCK_AFTER - 1):
        with pytest.raises(A.AccountError) as ei:
            A.verify_code(acct, "ZZZZZZZZ", NOW)
        assert f"还可以试 {A.LOCK_AFTER - 1 - i} 次" in str(ei.value)
    with pytest.raises(A.AccountError) as ei:
        A.verify_code(acct, "ZZZZZZZZ", NOW)
    assert "锁定" in str(ei.value)
    # 锁定期内即使输对也不放行 (否则暴力枚举只是慢一点)
    good = acct.pending[0].code
    with pytest.raises(A.AccountError) as ei2:
        A.verify_code(acct, good, NOW + timedelta(minutes=1))
    assert "锁定" in str(ei2.value)
    # 锁到期后恢复正常: 重新签一个码就能用 (锁定不能被永久记住)
    later = NOW + A.LOCK_FOR + timedelta(seconds=1)
    fresh = A.issue_code(acct, later)
    assert A.verify_code(acct, fresh.code, later) is not None


def test_issuing_codes_is_rate_limited(book):
    for _ in range(A.RATE_MAX):
        book.issue_code(NOW)
    with pytest.raises(A.AccountError) as ei:
        book.issue_code(NOW + timedelta(seconds=30))
    assert "频繁" in str(ei.value)
    book.issue_code(NOW + A.RATE_WINDOW + timedelta(seconds=1))     # 过了窗口就恢复


def test_direction_must_match(store):
    """"手机端显示给电脑端"的码不能当"电脑端生成的码"用, 反过来也一样。"""
    acct = A.load(store)
    phone_code = A.issue_code(acct, NOW, A.ROLE_PHONE)
    with pytest.raises(A.AccountError, match="方向不对"):
        A.verify_code(acct, phone_code.code, NOW, role=A.ROLE_DESKTOP)
    assert A.verify_code(acct, phone_code.code, NOW, role=A.ROLE_PHONE) is not None
    assert A.verify_code(acct, phone_code.code, NOW) is not None       # 不限方向时都可用


# ---------------------------------------------------------------- 设备与令牌
def test_device_gets_a_token_that_survives_reconnect(book):
    _acct, item = book.issue_code(NOW)
    _acct, dev = book.bind(item.code, NOW, name="手机", platform="Android")
    assert dev.token.startswith("dev_")
    assert book.device_by_token(dev.token) is not None
    assert book.device_by_token("dev_乱写") is None


def test_revoke_invalidates_the_token(book):
    _acct, item = book.issue_code(NOW)
    _acct, dev = book.bind(item.code, NOW, name="手机", platform="Android")
    acct, removed = book.revoke(dev.id)
    assert removed is not None and acct.devices == []
    assert book.device_by_token(dev.token) is None, "撤销后旧令牌必须失效"


def test_rebinding_same_device_issues_a_new_token(book):
    _acct, i1 = book.issue_code(NOW)
    _acct, first = book.bind(i1.code, NOW, name="手机", platform="Android")
    _acct, i2 = book.issue_code(NOW + timedelta(minutes=6))
    _acct, again = book.bind(i2.code, NOW + timedelta(minutes=6), name="手机", platform="Android")
    assert again.id == first.id and len(book.account.devices) == 1
    assert again.token != first.token, "重新绑定必须换发令牌, 否则旧令牌一直能用"


# ---------------------------------------------------------------- 二维码载荷
def test_qr_payload_roundtrip_and_bare_code():
    acct = A.Account(account_id="HR-7K3M-9QDT", created_at=NOW.isoformat())
    code = A.issue_code(acct, NOW)
    payload = A.qr_payload(acct, code)
    assert A.parse_qr_payload(payload) == {"account_id": "HR-7K3M-9QDT", "code": code.code}
    # 手动绑定粘贴的是纯码也要认
    assert A.parse_qr_payload(A.format_code(code.code)) == {"account_id": "", "code": code.code}
    assert A.parse_qr_payload("完全不是码") is None
    assert A.parse_qr_payload("") is None


# ---------------------------------------------------------------- 共享入口
def test_account_book_does_not_lose_writes_between_two_holders(store):
    """设置面板与同步桥各持一份内存副本时, 后写的那份会把先写的整段覆盖 —— 这里钉住不会。"""
    panel = A.AccountBook(store)
    bridge = A.AccountBook(store)

    _acct, item = panel.issue_code(NOW)
    # 桥这边不知道面板刚签发的码 (它没重新读), 但它必须先读再写
    _acct, dev = bridge.bind(item.code, NOW, name="手机", platform="Android")
    assert dev.id

    fresh = A.load(store)
    assert any(p.code == item.code for p in fresh.pending), "面板签发的码被桥的写入覆盖了"
    assert len(fresh.devices) == 1


def test_housekeeping_forgets_old_codes_but_keeps_recent_reasons(store):
    acct = A.load(store)
    old = A.issue_code(acct, NOW - A.RETENTION - timedelta(minutes=1))
    fresh = A.issue_code(acct, NOW)
    A.housekeeping(acct, NOW)
    codes = [p.code for p in acct.pending]
    assert fresh.code in codes and old.code not in codes
