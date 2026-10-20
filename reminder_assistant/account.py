"""账户与设备绑定 (PRD 2.4) —— 纯逻辑, 不碰 Qt/网络。

客户测试反馈第 2 条是"绑定功能没有实现" —— 也就是 PRD 2.4 的账户体系整块没落地。
这里把**规则**和**界面/传输**分开:

  规则 (本文件)     识别码、绑定码的签发/校验/一次性、限频、错 N 次锁定、设备令牌、撤销
  界面  (ui/account_section.py)  设置面板里的"账户"模块 + 二维码
  传输  (sync_demo.py)          演示回环里手机端把码登记到电脑端 / 用码完成绑定

为什么要这么分: "一次性、短有效期、错 N 次锁定、撤销后令牌失效"这些语义只有不依赖
界面和网络才能逐条断言 (tests/test_account.py)。演示当天手点两下通过, 不代表这些规则在。

安全取向与设计文档一致 (docs/云端同步与数据模型设计.md):
  * 不做账号找回 —— 那串识别码就是唯一凭据, 所以用 secrets 生成, **不能**是可推算的时间戳;
  * 绑定码一次性 + 短有效期 + 限频签发 + 连错锁定;
  * 绑定成功后换发**设备令牌** (不是把识别码当令牌用), 撤销设备即令牌失效;
  * 日志里不打印内容/令牌 —— 只打设备名与状态。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger("reminder.account")

META_KEY = "account.v1"

# 识别码/绑定码的字母表: 去掉容易看错的 I L O U (Crockford Base32 思路)。
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

CODE_LEN = 8                  # 8 位, 展示成 XXXX-XXXX
CODE_TTL = timedelta(minutes=5)
RATE_WINDOW = timedelta(minutes=10)
RATE_MAX = 10                 # 10 分钟内最多签发 10 个码
LOCK_AFTER = 5                # 连错 5 次
LOCK_FOR = timedelta(minutes=5)
# 过期/已用的码保留这么久再删: 用户拿旧码来试时要能回一句准确的原因, 而不是"码不对"。
RETENTION = timedelta(minutes=20)

ROLE_DESKTOP = "desktop"      # 电脑端生成, 手机端使用
ROLE_PHONE = "phone"          # 手机端生成, 电脑端手动输入
ROLE_LABELS = {ROLE_DESKTOP: "电脑端生成", ROLE_PHONE: "手机端生成"}

QR_SCHEME = "handyreminder://bind"


class AccountError(RuntimeError):
    """绑定相关的可预期失败 (把原因直接给用户看)。"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class Device:
    id: str
    name: str
    platform: str
    token: str
    bound_at: str
    last_seen: str | None = None

    @property
    def short_token(self) -> str:
        return self.token[:6]


@dataclass
class PendingCode:
    code: str
    role: str
    issued_at: str
    expires_at: str
    used_at: str | None = None

    def expired(self, now: datetime) -> bool:
        return datetime.fromisoformat(self.expires_at) <= now

    def alive(self, now: datetime) -> bool:
        return self.used_at is None and not self.expired(now)


@dataclass
class Account:
    account_id: str
    created_at: str
    devices: list[Device] = field(default_factory=list)
    pending: list[PendingCode] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    locked_until: str | None = None

    # ---- 序列化 (存 meta 表的一行 JSON, 不动表结构, 也不参与迁移) ----
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Account":
        raw = json.loads(text)
        return cls(
            account_id=str(raw.get("account_id") or ""),
            created_at=str(raw.get("created_at") or ""),
            devices=[Device(**d) for d in raw.get("devices") or []],
            pending=[PendingCode(**p) for p in raw.get("pending") or []],
            failures=[str(x) for x in raw.get("failures") or []],
            locked_until=raw.get("locked_until"),
        )

    # ---- 展示 ----
    def device(self, device_id: str) -> Device | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def device_by_token(self, token: str) -> Device | None:
        return next((d for d in self.devices if d.token == token), None)

    def lock_remaining(self, now: datetime) -> timedelta:
        if not self.locked_until:
            return timedelta(0)
        left = datetime.fromisoformat(self.locked_until) - now
        return left if left > timedelta(0) else timedelta(0)


# ---------------------------------------------------------------- 生成 / 规范化


def random_account_id() -> str:
    """HR-XXXX-XXXX。用 secrets 而不是 random/time —— 这串东西是唯一凭据。"""
    body = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"HR-{body[:4]}-{body[4:]}"


def random_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


_LOOKALIKE = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def normalize_code(text: str) -> str:
    """用户输入的绑定码 -> 规范形式。

    手抄/粘贴时最常见的错是 0/O、1/I/L 混用 (扫出来是 0 却抄成 O), 所以按 Crockford
    的等价关系归一化再比对 —— 否则用户会得到"码不对"而其实是抄错了字符形状。
    """
    raw = (text or "").strip().upper().translate(_LOOKALIKE)
    return "".join(ch for ch in raw if ch in ALPHABET)


def format_code(code: str) -> str:
    c = normalize_code(code)
    return f"{c[:4]}-{c[4:]}" if len(c) == CODE_LEN else c


def valid_code(code: str) -> bool:
    return len(normalize_code(code)) == CODE_LEN


def random_device_token() -> str:
    return "dev_" + secrets.token_urlsafe(18)


# ---------------------------------------------------------------- 读写


def load(store) -> Account:
    """读出账户; 没有就**当场建一个并落盘** (识别码必须稳定, 每次启动都一样)。"""
    raw = store.get_meta(META_KEY)
    if raw:
        try:
            acct = Account.from_json(raw)
            if acct.account_id:
                return acct
            log.warning("账户记录里没有识别码, 重新生成")
        except (ValueError, TypeError):
            log.warning("账户记录损坏, 重新生成 (旧记录被覆盖)")
    now = datetime.now().astimezone()
    acct = Account(account_id=random_account_id(), created_at=now.isoformat())
    save(store, acct)
    log.info("已创建账户识别码 %s", acct.account_id)
    return acct


def save(store, acct: Account) -> None:
    store.set_meta(META_KEY, acct.to_json())


# ---------------------------------------------------------------- 绑定码


def issue_code(acct: Account, now: datetime, role: str = ROLE_DESKTOP) -> PendingCode:
    """签发一个绑定码。限频 + 一次只留少量有效码。"""
    if role not in ROLE_LABELS:
        raise AccountError(f"不认识的绑定码来源: {role}")
    housekeeping(acct, now)
    recent = [p for p in acct.pending
              if now - datetime.fromisoformat(p.issued_at) < RATE_WINDOW]
    if len(recent) >= RATE_MAX:
        raise AccountError("签发太频繁，请稍后再试（每个码只能用一次）")
    code = random_code()
    while any(p.code == code for p in acct.pending):        # 理论上不会撞, 但便宜
        code = random_code()
    item = PendingCode(code=code, role=role, issued_at=now.isoformat(),
                       expires_at=(now + CODE_TTL).isoformat())
    acct.pending.append(item)
    if len(acct.pending) > 40:                     # 保险: 极端情况下别让账户记录无限长
        acct.pending = acct.pending[-40:]
    log.info("签发绑定码 (%s), %s 后失效", ROLE_LABELS[role], CODE_TTL)
    return item


def housekeeping(acct: Account, now: datetime) -> int:
    """清理**早就该忘掉**的东西。

    注意不能一过期就删: 用户把用过的/过期的码再输一遍时, 我们要能回答"这个码已经用过了"
    或"已过期", 而不是笼统地说"码不对" —— 这两种失败的原因完全不同, 而后者会让用户不停重试
    (最后把账户锁掉)。所以过期/已用的码保留 RETENTION 再删。
    """
    before = len(acct.pending)
    cutoff = now - RETENTION
    acct.pending = [p for p in acct.pending
                    if datetime.fromisoformat(p.issued_at) > cutoff]
    cut = now - LOCK_FOR
    acct.failures = [f for f in acct.failures if datetime.fromisoformat(f) > cut]
    if acct.locked_until and datetime.fromisoformat(acct.locked_until) <= now:
        acct.locked_until = None
    return before - len(acct.pending)


def pending_codes(acct: Account, now: datetime) -> list[PendingCode]:
    housekeeping(acct, now)
    return [p for p in acct.pending if p.alive(now)]


def verify_code(acct: Account, raw: str, now: datetime, role: str | None = None) -> PendingCode:
    """校验绑定码。失败抛 AccountError (带对用户有意义的解释)。"""
    left = acct.lock_remaining(now)
    if left > timedelta(0):
        mins = max(1, int(left.total_seconds() // 60) + 1)
        raise AccountError(f"连续输错太多次，已锁定，请 {mins} 分钟后再试")
    housekeeping(acct, now)
    code = normalize_code(raw)
    if not code:
        raise AccountError("请填写绑定码")
    for p in acct.pending:
        if p.code != code:
            continue
        if p.used_at is not None:
            raise AccountError("这个绑定码已经用过了，请重新生成一个")   # 一次性
        if p.expired(now):
            raise AccountError("绑定码已过期，请重新生成一个")
        if role and p.role != role:
            raise AccountError(f"这个码是{ROLE_LABELS[p.role]}的，方向不对")
        acct.failures.clear()
        return p
    # 输错了: 记一次失败, 连错 LOCK_AFTER 次就锁
    acct.failures.append(now.isoformat())
    if len(acct.failures) >= LOCK_AFTER:
        acct.locked_until = (now + LOCK_FOR).isoformat()
        acct.failures.clear()
        raise AccountError(f"连续输错 {LOCK_AFTER} 次，已锁定 {int(LOCK_FOR.total_seconds() // 60)} 分钟")
    left_tries = LOCK_AFTER - len(acct.failures)
    raise AccountError(f"绑定码不对，还可以试 {left_tries} 次")


def confirm_bind(acct: Account, raw: str, now: datetime, *, name: str, platform: str = "未知",
                 role: str | None = None, token: str | None = None) -> Device:
    """校验 + 一次性作废 + 建/更新设备 + 换发令牌。调用方负责 save()。

    同一台设备再次绑定时**换发新令牌** (旧令牌立刻失效): 否则"重新绑定"只是多了一条
    记录, 而用户以为旧的绑定已经作废了。
    """
    item = verify_code(acct, raw, now, role=role)
    item.used_at = now.isoformat()
    existing = next((d for d in acct.devices if d.name == name and d.platform == platform), None)
    if existing is not None:
        existing.token = token or random_device_token()
        existing.bound_at = now.isoformat()
        existing.last_seen = now.isoformat()
        log.info("设备重新绑定: %s", name)
        return existing
    dev = Device(id=secrets.token_hex(6), name=name or "未命名设备", platform=platform,
                 token=token or random_device_token(), bound_at=now.isoformat(),
                 last_seen=now.isoformat())
    acct.devices.append(dev)
    log.info("设备已绑定: %s (%s), 共 %d 台", dev.name, platform, len(acct.devices))
    return dev


def revoke_device(acct: Account, device_id: str) -> Device | None:
    """撤销绑定: 令牌随之作废 (返回被撤销的设备, 找不到就是 None)。"""
    dev = acct.device(device_id)
    if dev is None:
        dev = next((d for d in acct.devices if d.token.startswith(device_id)), None)
    if dev is None:
        return None
    acct.devices.remove(dev)
    log.info("已撤销设备绑定: %s", dev.name)
    return dev


# ---------------------------------------------------------------- 二维码载荷


def qr_payload(acct: Account, code: PendingCode) -> str:
    """二维码里放的东西: 识别码 + 一次性绑定码。手机端扫码后解析这段文本即可。"""
    return f"{QR_SCHEME}?v=1&acc={acct.account_id}&code={code.code}"


def parse_qr_payload(text: str) -> dict | None:
    """把扫码结果 (或用户粘进来的一整段) 解析成 {account_id, code}。

    容错: 直接粘贴纯绑定码也要能用 —— 这才是"手动绑定"的实际输入。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.lower().startswith(QR_SCHEME):
        query = raw.split("?", 1)[1] if "?" in raw else ""
        fields = {}
        for part in query.split("&"):
            k, _, v = part.partition("=")
            fields[k.strip()] = v.strip()
        code = normalize_code(fields.get("code", ""))
        acc = fields.get("acc", "").strip()
        if not valid_code(code):
            return None
        return {"account_id": acc, "code": code}
    code = normalize_code(raw)
    if valid_code(code):
        return {"account_id": "", "code": code}
    return None


def describe(acct: Account, now: datetime) -> dict:
    """给界面/CLI 用的一屏摘要 (不含令牌明文)。"""
    alive = pending_codes(acct, now)
    return {
        "account_id": acct.account_id,
        "devices": [{"id": d.id, "name": d.name, "platform": d.platform,
                     "bound_at": d.bound_at} for d in acct.devices],
        "pending": [{"code": format_code(p.code), "role": p.role,
                     "ttl_s": int((datetime.fromisoformat(p.expires_at) - now).total_seconds())}
                    for p in alive],
        "locked_s": int(acct.lock_remaining(now).total_seconds()),
    }


# ---------------------------------------------------------------- 共享入口


class AccountBook:
    """账户的**唯一入口**: 每次改动都是"重新读盘 -> 改 -> 写回"。

    为什么不能各持一份内存副本: 设置面板和同步桥都要改账户。如果两边各自 load() 一份,
    面板刚签发的绑定码会被桥那边稍后的一次 save() 整段覆盖 —— 表现是"刚生成的码手机端
    说不对/不存在", 而且两边看着都正常。所以所有写操作都走这里, 并且在锁里先 reload。
    """

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._acct = load(store)

    @property
    def account(self) -> Account:
        return self._acct

    def reload(self) -> Account:
        with self._lock:
            self._acct = load(self.store)
            return self._acct

    def issue_code(self, now: datetime, role: str = ROLE_DESKTOP) -> tuple[Account, PendingCode]:
        with self._lock:
            acct = self.reload()
            item = issue_code(acct, now, role)
            save(self.store, acct)
            return acct, item

    def bind(self, raw: str, now: datetime, *, name: str, platform: str = "未知",
             role: str | None = None) -> tuple[Account, Device]:
        with self._lock:
            acct = self.reload()
            dev = confirm_bind(acct, raw, now, name=name, platform=platform, role=role)
            save(self.store, acct)
            return acct, dev

    def revoke(self, device_id: str) -> tuple[Account, Device | None]:
        with self._lock:
            acct = self.reload()
            dev = revoke_device(acct, device_id)
            save(self.store, acct)
            return acct, dev

    def touch(self, now: datetime) -> Account:
        """顺手清理过期码/过期锁定, 并把结果写回。"""
        with self._lock:
            acct = self.reload()
            housekeeping(acct, now)
            save(self.store, acct)
            return acct

    def device_by_token(self, token: str) -> Device | None:
        """按设备令牌认设备 (手机端重连时用它恢复绑定, 不必再输一遍码)。

        撤销设备后再拿旧令牌来认就会返回 None —— 这正是"撤销"要有的效果。
        """
        if not token:
            return None
        acct = self.reload()
        return acct.device_by_token(token)
