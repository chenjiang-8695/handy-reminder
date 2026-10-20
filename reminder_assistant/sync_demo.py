"""展示版"假双端"同步桥 —— 给客户看的那一版, 不是正式同步实现。

演示动线
    PC 上圈选/新建  ->  "手机"(真机浏览器)上立刻出现同一张卡
    手机上点"完成"  ->  PC 便签真的划掉 (不是两套状态互相假装)
    手机上加一条    ->  PC 端用同一套时间解析建卡
    手机上「属性」  ->  PC 端改属性 + 触发点按新时间重排 (PRD 2.5 事件属性)
    手机上「＋子任务」-> PC 端在主子任务下建真提醒, 开始时间按已有子任务顺推 (PRD 2.5 二级目录)

刻意保留正式版的形状 (snapshot + delta + 单调 seq), 所以演示时讲的话在正式版里依然成立;
但有两处明确简化, **对客户要讲明**:

  1. "云端"就是这个桌面端进程, 靠局域网可达 —— 没有公网服务器, 没有账号体系;
  2. PC 侧变更用 0.5s 轮询桥接给手机 —— 正式版是写路径直接进 outbox 再推送。

正式实现见 docs/云端同步与数据模型设计.md (云端唯一事实源 / server_seq 定序 /
base_version 乐观锁 / outbox 离线重放 / 墓碑优先), 本文件一行都不该被当成那个实现。

跑法
    python -m reminder_assistant --demo-sync                  # 便签窗口 + 同步桥
    python -m reminder_assistant --demo-sync --headless       # 只跑桥 (无窗口)
    手机浏览器打开打印出来的 http 地址即可
"""

from __future__ import annotations

import http.server
import json
import logging
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocket, QWebSocketServer
from PySide6.QtWidgets import QApplication

from .models import Reminder, ReminderPlan
from .tasks import chain_times
from .timeparse import parse_time
from . import account as acct

log = logging.getLogger("reminder.demo_sync")


# ====================================================================== 纯逻辑
# 这一段不碰 Qt 也不碰 socket —— 单测可以直接驱动它, 演示当天的行为提前钉死。

def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def view_rows(store, life, now: datetime | None = None) -> list[dict]:
    """桌面端当前一屏 -> 手机端要看到的数据。uuid 是对外唯一标识。

    顺序与电脑端便签一致 (主任务后面紧跟它的子任务), 并带上 depth/duration_min ——
    手机端因此也能显示二级目录, 而不是把子任务当普通任务平铺。
    """
    rows: list[dict] = []
    for it in life.board(now)["active"]:
        rem = store.get_reminder(it["id"])
        if rem is None:                       # 轮询间隙里被删掉了
            continue
        rows.append({
            "uuid": rem.uuid, "title": rem.title, "level": rem.level, "status": rem.status,
            "event_at": _iso(it["event_at"]), "next_fire": _iso(it["next_fire"]),
            "fired": it["fired"], "total": it["total"], "overdue": bool(it["overdue"]),
            "depth": int(it.get("depth") or 0), "parent_uuid": rem.parent_uuid,
            "duration_min": int(rem.duration_min or 0),
            "child_count": int(it.get("child_count") or 0),
            "note": rem.note or "",
            "version": 1,
        })
    return rows


# ---- 手机端能改的属性 ----------------------------------------------------
# 与桌面端 ui/settings_dialog.collect_attributes 对齐: 手机端不是在维护第二份模型,
# 它发上来的是同一组字段名, 落库走的也是同一个 Store.update_attributes。
ATTR_FIELDS: tuple[str, ...] = ("title", "event_at", "level", "duration_min", "note")
MAX_DURATION_MIN = 24 * 60


def _aware(text: str, tz) -> datetime | None:
    """手机端给的时刻 -> aware datetime。

    浏览器 <input type="datetime-local"> 交上来的是**没有时区的钟面时间**, 直接当 UTC
    存就会偏一个时区 (用户改完 18:00 的会, 提醒落在 02:00)。这里按这条提醒原来的
    时区回填 —— 与桌面端 QDateTimeEdit 的 from_qdatetime 是同一个道理。
    """
    s = (text or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def parse_attr_fields(raw: dict, current: dict, *, levels: Iterable[str] = ()) -> tuple[dict, str | None]:
    """手机端提交的属性 -> `Store.update_attributes` 能吃的字段 + 给用户看的错误。

    返回 (fields, None) 或 ({}, 错误文本)。纯函数 (不碰 Qt / 不碰库), 所以
    "哪些字段合法、什么算真的改过" 能逐条断言, 不用等演示当天发现。

    只挑真的改过的字段: 原样提交也写一遍会平白触发一次触发点重排。
    """
    allowed = tuple(levels) or ("日常", "重要", "紧急")
    ev_iso = str(current.get("event_at") or "")
    old_event = datetime.fromisoformat(ev_iso) if ev_iso else None
    tz = (old_event.tzinfo if old_event else None) or datetime.now().astimezone().tzinfo
    out: dict = {}

    if "title" in raw:
        title = str(raw.get("title") or "").strip()
        if not title:
            return {}, "名称不能为空"
        if title != str(current.get("title") or ""):
            out["title"] = title
    if "event_at" in raw:
        when = _aware(str(raw.get("event_at") or ""), tz)
        if when is None:
            return {}, "没看懂这个时间"
        if old_event is None or abs((when - old_event).total_seconds()) > 1:
            out["event_at"] = when
    if "level" in raw:
        level = str(raw.get("level") or "").strip()
        if level not in allowed:
            return {}, f"不认识的重要程度: {level or '(空)'}"
        if level != str(current.get("level") or ""):
            out["level"] = level
    if "duration_min" in raw:
        try:
            dur = int(raw.get("duration_min") or 0)
        except (TypeError, ValueError):
            return {}, "持续时间要填分钟数"
        if dur < 0 or dur > MAX_DURATION_MIN:
            return {}, f"持续时间要在 0 - {MAX_DURATION_MIN} 分钟之间"
        if dur != int(current.get("duration_min") or 0):
            out["duration_min"] = dur
    if "note" in raw:
        note = str(raw.get("note") or "").strip()
        if note != str(current.get("note") or ""):
            out["note"] = note
    return out, None


class DemoHub:
    """两端共享的那份"真相"的镜像 + 消息构造。seq 单调, 与正式版 server_seq 同形。"""

    def __init__(self, levels: Iterable[str] = ()) -> None:
        self.seq = 0
        self.tasks: dict[str, dict] = {}
        # 手机端属性面板的"重要程度"下拉要跟电脑端一致, 所以随快照一起发过去。
        self.levels = list(levels) or ["日常", "重要", "紧急"]

    # ---------------------------------------------------------------- 出向
    def snapshot(self) -> dict:
        return {"t": "snapshot", "seq": self.seq, "levels": list(self.levels),
                "tasks": [self.tasks[u] for u in self._order()]}

    def _order(self) -> list[str]:
        return sorted(self.tasks, key=lambda u: (self.tasks[u].get("next_fire") or "~", u))

    def refresh(self, rows: Iterable[dict]) -> list[dict]:
        """用桌面端最新一屏刷新镜像, 返回需要广播的 delta(没有变化就是空表)。"""
        msgs: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            u = row.get("uuid") or ""
            if not u:
                continue
            seen.add(u)
            old = self.tasks.get(u)
            if old is None:
                self.tasks[u] = dict(row)
                msgs.append(self._delta("upsert", self.tasks[u]))
                continue
            # version 是本镜像自己的记账字段, 不是桌面端那一屏的内容 —— 把它放进比较
            # 里就永远"有变化", 于是每 0.5 秒广播一次 delta、手机端跟着重绘一次。
            if any(old.get(k) != row.get(k) for k in row if k != "version"):
                merged = dict(old, **row)
                merged["version"] = int(old.get("version", 1)) + 1
                self.tasks[u] = merged
                msgs.append(self._delta("upsert", merged))
        for gone in [u for u in self.tasks if u not in seen]:
            self.tasks.pop(gone, None)
            msgs.append(self._delta("remove", {"uuid": gone}))
        return msgs

    def _delta(self, ev: str, task: dict) -> dict:
        self.seq += 1
        return {"t": "delta", "seq": self.seq, "ev": ev, "task": task}

    # ---------------------------------------------------------------- 入向
    def apply_op(self, msg: dict) -> list[dict]:
        """手机端来的操作 -> 要桌面端执行的动作。纯函数, 不改自己的状态。

        所有变更都下沉到桌面端的同一套 Store/Lifecycle 上, 所以"手机端点完成、
        电脑端真的划掉"是真事; 这里不自己维护第二份任务状态 (那正是 PRD 风险 2 说的坑)。
        """
        op = str(msg.get("op") or "")
        u = str(msg.get("uuid") or "")
        if op == "add":
            text = str(msg.get("text") or "").strip()
            if not text:
                return [{"op": "reject", "reason": "内容不能为空"}]
            return [{"op": "add", "text": text}]
        if op in ("complete", "delete", "snooze"):
            if u not in self.tasks:
                return [{"op": "reject", "reason": "这条任务不在了, 可能已在电脑端删掉"}]
            return [{"op": op, "uuid": u}]
        if op == "attr":
            if u not in self.tasks:
                return [{"op": "reject", "reason": "这条任务不在了, 可能已在电脑端删掉"}]
            raw = msg.get("fields")
            if not isinstance(raw, dict):
                return [{"op": "reject", "reason": "没收到要改的属性"}]
            fields, err = parse_attr_fields(raw, self.tasks[u], levels=self.levels)
            if err:
                return [{"op": "reject", "reason": err}]
            if not fields:
                return [{"op": "reject", "reason": "什么都没改"}]
            return [{"op": "attr", "uuid": u, "fields": fields}]
        if op == "add_child":
            if u not in self.tasks:
                return [{"op": "reject", "reason": "这条主任务不在了, 可能已在电脑端删掉"}]
            if int(self.tasks[u].get("depth") or 0) >= 1:
                return [{"op": "reject", "reason": "只支持二级: 子任务下面不能再建子任务"}]
            text = str(msg.get("text") or "").strip()
            if not text:
                return [{"op": "reject", "reason": "子任务还没有名字"}]
            try:
                dur = int(msg.get("duration_min") or 0)
            except (TypeError, ValueError):
                return [{"op": "reject", "reason": "持续时间要填分钟数"}]
            if dur < 0 or dur > MAX_DURATION_MIN:
                return [{"op": "reject", "reason": f"持续时间要在 0 - {MAX_DURATION_MIN} 分钟之间"}]
            return [{"op": "add_child", "uuid": u, "text": text, "duration_min": dur}]
        if op == "bind":
            # 手机端用电脑端(或它自己)签发的绑定码完成绑定 (PRD 2.4)。
            raw = str(msg.get("code") or "").strip()
            if not acct.valid_code(raw):
                return [{"op": "reject", "reason": "绑定码看着不对：应该是 8 位（如 AB3D-7F9K）"}]
            return [{"op": "bind", "code": acct.normalize_code(raw),
                     "name": str(msg.get("name") or "手机端").strip() or "手机端"}]
        if op == "phone_code":
            # 手机端自己生成绑定码, 由用户在电脑端手动输入 (PRD 2.4 的"手动绑定")。
            return [{"op": "phone_code"}]
        if op == "resume":
            # 重连时用设备令牌恢复绑定 (撤销过的令牌到这里就会被拒)。
            token = str(msg.get("token") or "").strip()
            if not token:
                return [{"op": "reject", "reason": "没带设备令牌"}]
            return [{"op": "resume", "token": token}]
        return [{"op": "reject", "reason": f"不认识的操作: {op or '(空)'}"}]


# ====================================================================== Qt 传输

class SyncDemoServer(QObject):
    """WebSocket 桥: 手机浏览器 <-> 桌面端。"""

    client_count = Signal(int)
    log_line = Signal(str)

    def __init__(self, store, life, *, port: int = 8765, clock=None,
                 interval_ms: int = 500, levels: Iterable[str] = (),
                 plan_for=None, account=None, require_bind: bool = False,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        if QApplication.instance() is None:
            # 没有 QApplication 就没有事件分发器: 端口会听、TCP 能连上, 但握手永远不回。
            # 这种失败是静默的, 必须在这里拦下来。
            raise RuntimeError("SyncDemoServer 必须在 QApplication 之后创建")
        self.store, self.life, self.clock = store, life, clock
        # 等级 -> 提醒频率的配方 (电脑端设置面板里改过的那份)。手机端把等级从
        # 「日常」改成「紧急」时, 频率必须跟着换, 否则卡片写着紧急却只响一次。
        self.plan_for = plan_for
        # 账户 (AccountBook)。有了它就要求手机端先绑定 (PRD 2.4 的用户路径:
        # "手机端打开 App 扫码, 绑定成功, 双端开始实时同步"); 没有就退回"免绑定"的
        # 纯演示模式 —— 单测与旧用法都走这条路, 行为不变。
        self.account = account
        self.require_bind = bool(require_bind and account is not None)
        self._bound: set = set()
        self._first_snapshot_sent: dict[int, bool] = {}
        self.hub = DemoHub(levels)
        self._clients: list[QWebSocket] = []
        self.server = QWebSocketServer("handyreminder-demo", QWebSocketServer.NonSecureMode, self)
        if not self.server.listen(QHostAddress.Any, port):
            raise RuntimeError(f"同步桥端口 {port} 起不来: {self.server.errorString()}")
        self.port = int(self.server.serverPort())
        self.server.newConnection.connect(self._on_new_connection)
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll(announce=False)             # 先把当前一屏装进镜像

    # ---------------------------------------------------------------- 桌面端 -> 手机
    def poll(self, announce: bool = True) -> list[dict]:
        now = self.clock.now() if self.clock else None
        msgs = self.hub.refresh(view_rows(self.store, self.life, now))
        if announce and msgs:
            self._broadcast(msgs)
        return msgs

    def _broadcast(self, msgs: Iterable[dict]) -> None:
        for m in msgs:
            self.send_to_all(m)

    def send_to_all(self, msg: dict) -> None:
        payload = json.dumps(msg, ensure_ascii=False)
        for ws in list(self._clients):
            try:
                ws.sendTextMessage(payload)
            except Exception:                 # 客户端刚断, 下一条消息会清理它
                log.debug("发送失败, 跳过该客户端", exc_info=True)

    # ---------------------------------------------------------------- 手机 -> 桌面端
    def _on_new_connection(self) -> None:
        ws = self.server.nextPendingConnection()
        if ws is None:
            return
        self._clients.append(ws)
        ws.textMessageReceived.connect(lambda text, sock=ws: self._on_text(sock, text))
        ws.disconnected.connect(lambda sock=ws: self._drop(sock))
        if self.require_bind:
            # 没绑定就先只打个招呼: 任务列表是"绑定之后才看得见"的东西,
            # 先推快照等于把没绑定的连接也当成了合法手机端。
            ws.sendTextMessage(json.dumps(
                {"t": "hello", "need_bind": True,
                 "account_id": self.account.account.account_id}, ensure_ascii=False))
            self._emit_quietly(self.log_line, "手机端接入, 等待绑定 (尚未推送任务)")
        else:
            ws.sendTextMessage(json.dumps(self.hub.snapshot(), ensure_ascii=False))
        self._emit_quietly(self.client_count, len(self._clients))
        if not self.require_bind:
            self._emit_quietly(self.log_line,
                               f"手机端接入 (共 {len(self._clients)} 台), 已推送快照 seq={self.hub.seq}")

    def _drop(self, ws: QWebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)
        self._bound.discard(ws)
        self._emit_quietly(self.client_count, len(self._clients))
        self._emit_quietly(self.log_line, f"手机端断开 (剩 {len(self._clients)} 台)")

    @staticmethod
    def _emit_quietly(signal, *args) -> None:
        """关闭过程中 C++ 侧可能已经析构, 信号发不出去不该把演示打断。"""
        try:
            signal.emit(*args)
        except RuntimeError:
            log.debug("信号发送失败 (对象正在析构)", exc_info=True)

    def _on_text(self, ws: QWebSocket, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            ws.sendTextMessage(json.dumps({"t": "notice", "text": "消息不是合法 JSON"}))
            return
        op = str(msg.get("op") or "")
        if self.require_bind and ws not in self._bound and op not in ("bind", "phone_code", "resume"):
            ws.sendTextMessage(json.dumps(
                {"t": "notice",
                 "text": "还没绑定：请用电脑端「设置 → 账户与设备绑定」里的绑定码完成绑定，"
                         "绑定后任务才会同步过来。"}, ensure_ascii=False))
            return
        actions = self.hub.apply_op(msg)
        try:
            replies = self._execute(actions, ws)
        except Exception as exc:                  # noqa: BLE001 - 手机端点错一下不该把桥打挂
            log.exception("手机端操作执行失败")
            replies = [{"t": "notice", "text": f"电脑端执行失败: {exc}"}]
        for m in replies:
            try:
                ws.sendTextMessage(json.dumps(m, ensure_ascii=False))
            except Exception:                     # noqa: BLE001 - 客户端刚断
                log.debug("回包失败", exc_info=True)
        if self.require_bind and ws in self._bound and not self._first_snapshot_sent.get(id(ws)):
            # 刚绑定成功: 这台客户端还没见过任务列表, 补一份完整快照 (后面的都走 delta)
            self._first_snapshot_sent[id(ws)] = True
            ws.sendTextMessage(json.dumps(self.hub.snapshot(), ensure_ascii=False))
        self.poll(announce=True)              # 真改完之后立刻把结果广播给所有人

    # ---------------------------------------------------------------- 落地执行
    def _execute(self, actions: Iterable[dict], ws=None) -> list[dict]:
        """执行手机端来的动作, 返回要回给**这台**客户端的消息 (notice / bound / pair_code)。

        返回消息列表而不是单个字符串, 是因为"绑定"要回结构化的东西 (设备令牌),
        而"改属性"只要一句提示 —— 用同一条通道, 就不必给绑定开第二条协议。
        """
        out: list[dict] = []

        def notice(text: str) -> None:
            out.append({"t": "notice", "text": text})

        for act in actions:
            op = act["op"]
            if op == "reject":
                notice(str(act.get("reason") or "操作被拒绝"))
                return out
            if op == "add":
                msg = self._do_add(str(act["text"]))
                if msg:
                    notice(msg)
                continue
            if op == "bind":
                out.extend(self._do_bind(act, ws))
                continue
            if op == "resume":
                out.extend(self._do_resume(act, ws))
                continue
            if op == "phone_code":
                out.extend(self._do_phone_code())
                continue
            rid = self.store.id_of_uuid(str(act["uuid"]))
            if rid is None:
                notice("这条任务不在了, 可能已在电脑端删掉")
                return out
            if op == "complete":
                self.life.complete(rid)
                self.log_line.emit(f"手机端点完成 -> 电脑端划掉 #{rid}")
            elif op == "delete":
                self.life.remove(rid)         # 连带子任务一起删 (主任务没了, 子任务不该还在响)
                self.log_line.emit(f"手机端删除 -> 电脑端移除 #{rid}")
            elif op == "snooze":
                when = self.life.snooze(rid, 0, 5)
                self.log_line.emit(f"手机端稍后 -> #{rid} 改到 {when:%H:%M}")
            elif op == "attr":
                # 手机端改的是**同一组属性字段**, 落库走的也是同一个 update_attributes:
                # 改时间在这里会连带重排触发点 (并平移子任务), 不是只改个字段。
                updated = self.life.update_attributes(rid, **act["fields"])
                changed = " ".join(k for k in ATTR_FIELDS if k in act["fields"])
                self.log_line.emit(f"手机端改属性 -> 电脑端 #{rid} {changed}"
                                   f" (事件时间 {updated.event_at:%m-%d %H:%M})")
                if "level" in act["fields"]:
                    # 与桌面端面板同一个顺序: 先落属性, 再按等级重排频率。改了等级不换
                    # 频率的话, 卡片写着「紧急」却只提醒一次。
                    plan = (self.plan_for(updated.level) if self.plan_for
                            else ReminderPlan.preset(updated.level))
                    self.life.change_plan(rid, plan)
                    self.log_line.emit(f"手机端改等级 -> #{rid} 按「{updated.level}」"
                                       f"重排 {len(plan.offsets)} 次提醒")
            elif op == "add_child":
                parent = self.store.get_reminder(rid)
                if parent is None:
                    notice("这条主任务不在了, 可能已在电脑端删掉")
                    return out
                when = self._next_child_start(parent)
                child = self.life.add_child(parent, title=str(act["text"]),
                                            duration_min=int(act["duration_min"]),
                                            event_at=when)
                self.log_line.emit(f"手机端建子任务 -> 电脑端 #{child.id} 挂在 #{rid} 下"
                                   + (f" 开始 {when:%m-%d %H:%M}" if when else ""))
        return out

    # ---------------------------------------------------------------- 绑定 (2.4)
    def _do_bind(self, act: dict, ws) -> list[dict]:
        """手机端用绑定码完成绑定: 校验走 account 的那套规则 (一次性/有效期/限频/锁定)。"""
        if self.account is None:
            return [{"t": "notice", "text": "这台电脑没开账户模块, 手机端先不用绑定"}]
        now = self.clock.now() if self.clock else datetime.now().astimezone()
        name = str(act.get("name") or "手机端")
        try:
            # 手机端扫的是电脑端生成的码 —— 方向必须对上 (防止把手机端自己生成的码
            # 当成"扫码绑定"的码用, 那会让"只允许一次"的语义变得含糊)。
            _acct, dev = self.account.bind(act["code"], now, name=name, platform="手机端",
                                           role=acct.ROLE_DESKTOP)
        except acct.AccountError as exc:
            self.log_line.emit(f"手机端绑定被拒: {exc}")
            return [{"t": "notice", "text": str(exc)}]
        if ws is not None:
            self._bound.add(ws)
        self.log_line.emit(f"手机端绑定成功 -> 电脑端设备列表新增「{dev.name}」")
        return [{"t": "bound", "device": {"id": dev.id, "name": dev.name,
                                          "platform": dev.platform, "token": dev.token},
                 "account_id": _acct.account_id},
                {"t": "notice", "text": f"绑定成功：{dev.name}。任务列表已开始同步。"}]

    def _do_resume(self, act: dict, ws) -> list[dict]:
        """手机端带设备令牌重连: 令牌有效就直接恢复绑定, 不用再输一次码。"""
        if self.account is None:
            return [{"t": "notice", "text": "这台电脑没开账户模块"}]
        dev = self.account.device_by_token(str(act.get("token") or ""))
        if dev is None:
            return [{"t": "notice",
                     "text": "之前的绑定已失效（可能已在电脑端撤销），请重新用绑定码绑定"}]
        if ws is not None:
            self._bound.add(ws)
        self.log_line.emit(f"手机端凭设备令牌恢复绑定: {dev.name}")
        return [{"t": "bound", "device": {"id": dev.id, "name": dev.name,
                                         "platform": dev.platform, "token": dev.token},
                 "account_id": self.account.account.account_id, "resumed": True}]

    def _do_phone_code(self) -> list[dict]:
        """手机端自己生成绑定码, 让用户在电脑端「手动绑定」里输入。"""
        if self.account is None:
            return [{"t": "notice", "text": "这台电脑没开账户模块, 不能生成绑定码"}]
        now = self.clock.now() if self.clock else datetime.now().astimezone()
        try:
            _acct, item = self.account.issue_code(now, acct.ROLE_PHONE)
        except acct.AccountError as exc:
            return [{"t": "notice", "text": str(exc)}]
        self.log_line.emit("手机端生成了一个绑定码 (待用户在电脑端输入)")
        return [{"t": "pair_code", "code": acct.format_code(item.code),
                 "ttl_s": int(acct.CODE_TTL.total_seconds())},
                {"t": "notice",
                 "text": "把这个码填到电脑端：设置 → 账户与设备绑定 → 输入框 → 绑定"}]

    def _next_child_start(self, parent) -> datetime | None:
        """新子任务的开始时刻 = 上一个子任务的开始 + 它的持续时间。

        与桌面端向导的顺推是**同一套算法** (tasks.chain_times)。这件派生计算必须放在
        服务端: 手机端只发"名字 + 持续分钟", 让它自己算就会在"中途又插一个"时错位 ——
        症状是一批子任务全挤在同一时刻, 而界面用例照样全绿。
        """
        if parent.event_at is None:
            return None
        kids = self.store.children_of(parent.uuid)
        durs = [int(k.duration_min or 0) for k in kids]
        starts = chain_times(parent.event_at, durs)
        if not starts or starts[-1] is None:
            return parent.event_at
        return starts[-1] + timedelta(minutes=durs[-1])

    def _do_add(self, text: str) -> str | None:
        now = self.clock.now() if self.clock else datetime.now().astimezone()
        parsed = parse_time(text, now=now)
        if parsed.when is None:
            return "没识别出时间, 写成「明天下午三点开会」这样试试"
        rem = Reminder(title=text, source_text=text, event_at=parsed.when, created_at=now,
                       level="日常", plan=ReminderPlan.preset("日常"))
        rem = self.life.create(rem, now)
        self.log_line.emit(f"手机端新建 -> 电脑端建卡 #{rem.id} {parsed.when:%m-%d %H:%M}")
        return None

    def close(self) -> None:
        self.timer.stop()
        for ws in list(self._clients):
            ws.close()
        self._clients.clear()
        self.server.close()


# ====================================================================== 手机端网页

def page_html(ws_port: int) -> str:
    """手机端那一页。单文件, 不引任何 CDN —— 断网也得能演示。"""
    return _PAGE.replace("__WS_PORT__", str(ws_port))


class _PageHandler(http.server.BaseHTTPRequestHandler):
    html = ""
    server_version = "HandyReminderDemo/1.0"

    def do_GET(self):                          # noqa: N802 - BaseHTTPRequestHandler 的接口
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404, "only / is served")
            return
        body = self.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):         # 别把每个请求都打到控制台
        log.debug("page: " + fmt, *args)


class PageServer:
    """手机端网页的 HTTP 服务 (独立线程, 与 Qt 事件循环无关)。"""

    def __init__(self, port: int, ws_port: int) -> None:
        handler = type("_H", (_PageHandler,), {"html": page_html(ws_port)})
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="demo-page", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def local_ip() -> str:
    """当前局域网地址 (不产生真实流量: UDP connect 不握手)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@dataclass
class DemoBundle:
    server: SyncDemoServer
    pages: PageServer
    phone_url: str
    local_url: str

    def close(self) -> None:
        self.server.close()
        self.pages.close()


def start_demo_sync(app, *, ws_port: int = 8765, page_port: int = 8766) -> DemoBundle:
    """把同步桥 + 手机端网页挂到正在跑的 App 上。

    手机端**必须先绑定** (PRD 2.4 的用户路径: 扫码/输码 → 绑定成功 → 双端开始同步),
    所以这里把账户簿交给桥, 并打开 require_bind。
    """
    levels = app.settings.level_names()
    server = SyncDemoServer(app.store, app.life, port=ws_port, clock=app.clock,
                            levels=levels, plan_for=app.settings.plan,
                            account=app.account, require_bind=True)
    pages = PageServer(page_port, server.port)
    ip = local_ip()
    phone_url = f"http://{ip}:{pages.port}/"
    local_url = f"http://127.0.0.1:{pages.port}/"
    server.log_line.connect(lambda s: log.info("%s", s))
    account_id = app.account.reload().account_id
    print("演示同步桥已启动 (演示用回环, 不含公网服务器/推送):")
    print(f"  账户识别码   {account_id}     <- 手机端绑定的对象就是这台电脑")
    print(f"  手机端网页   {phone_url}      <- 手机连同一个 Wi-Fi 打开这个")
    print(f"  本机也能打开 {local_url}")
    print(f"  WebSocket    ws://{ip}:{server.port}/")
    print("  绑定方式     电脑端 ⚙ →「账户与设备绑定」→「生成绑定码」→ 手机端输码/扫码；"
          "也可以手机端点「生成我的绑定码」再把码填到电脑端。")
    print("  提示: 手机打不开多半是 Windows 防火墙拦了入站, 放行 Python 即可。")
    return DemoBundle(server=server, pages=pages, phone_url=phone_url, local_url=local_url)


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智能手账 · 手机端(演示)</title>
<style>
  :root { --ink:#33302b; --line:#d9d2c5; --paper:#fbf7ee; --accent:#b5651d; }
  * { box-sizing:border-box; }
  body { margin:0; padding:0 12px 28px; background:var(--paper); color:var(--ink);
         font-family:"KaiTi","楷体","LXGW WenKai",system-ui,sans-serif;
         background-image:repeating-linear-gradient(var(--paper) 0 27px, var(--line) 27px 28px); }
  header { padding:14px 2px 8px; }
  h1 { font-size:19px; margin:0 0 6px; letter-spacing:1px; }
  .stat { font-size:13px; color:#7b7265; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px;
         background:#c0392b; vertical-align:middle; }
  .dot.on { background:#2e8b57; }
  #add { display:flex; gap:8px; margin:10px 0 16px; }
  #text { flex:1; padding:11px 12px; font-size:16px; border:1px solid var(--line);
          border-radius:8px; background:rgba(255,255,255,.75); font-family:inherit; color:inherit; }
  #send { padding:0 16px; font-size:15px; border:0; border-radius:8px; background:var(--accent);
          color:#fff; font-family:inherit; }
  .card { background:rgba(255,255,255,.8); border:1px solid var(--line); border-left:5px solid var(--accent);
          border-radius:10px; padding:10px 12px; margin-bottom:10px;
          box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .card.done { opacity:.45; text-decoration:line-through; }
  .card .title { font-size:17px; font-weight:bold; line-height:1.35; }
  .card .meta { font-size:12px; color:#7b7265; margin:5px 0 9px; }
  .card .meta b { color:#b5651d; }
  .row { display:flex; gap:8px; }
  .row button { flex:1; padding:9px 0; font-size:14px; font-family:inherit;
                border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); }
  .row button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  .row button.danger { color:#a93226; }
  .card.child { margin-left:18px; border-left-color:#c9a227; padding:8px 10px; }
  .card .children { font-size:11px; color:#b5651d; margin-top:4px; }
  .card .note { font-size:12px; color:#7b7265; margin-top:4px; }
  .card .row + .row { margin-top:7px; }
  #empty { color:#8c8375; font-size:14px; padding:16px 4px; }
  #gate { display:none; }
  #gate .box { background:rgba(255,255,255,.85); border:1px solid var(--line);
               border-radius:12px; padding:14px; margin-top:8px; }
  #gate h2 { font-size:17px; margin:0 0 8px; }
  #gate ol { margin:0 0 12px 18px; padding:0; font-size:13px; color:#5a5245; line-height:1.7; }
  #mycode { font-size:22px; letter-spacing:3px; color:var(--accent); font-weight:bold;
            margin:6px 0; word-break:break-all; }
  #app { display:none; }
  #sheet { position:fixed; inset:0; background:rgba(30,28,24,.4); display:none;
           align-items:flex-end; z-index:20; }
  #sheet.show { display:flex; }
  .sheet-box { background:var(--paper); width:100%; max-height:88vh; overflow:auto;
               border-radius:14px 14px 0 0; padding:14px 14px 20px;
               box-shadow:0 -2px 14px rgba(0,0,0,.25); }
  .sheet-box h2 { font-size:16px; margin:0 0 12px; }
  .fld { display:block; font-size:13px; color:#7b7265; margin:0 0 10px; }
  .fld input, .fld select { display:block; width:100%; margin-top:4px; padding:10px 11px;
               font-size:16px; font-family:inherit; color:inherit; background:#fff;
               border:1px solid var(--line); border-radius:8px; }
  .hint { font-size:11px; color:#96907f; margin:2px 0 12px; line-height:1.5; }
  footer { margin-top:22px; font-size:11px; color:#96907f; line-height:1.6; }
  #toast { position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
           background:rgba(51,48,43,.92); color:#fff; padding:9px 14px; border-radius:8px;
           font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; }
  #toast.show { opacity:1; }
</style>
</head>
<body>
<header>
  <h1>智能手账 · 手机端</h1>
  <div class="stat"><span id="dot" class="dot"></span><span id="state">连接中…</span>
    · 同步序号 <b id="seq">0</b> · 任务 <b id="n">0</b></div>
</header>

<div id="gate">
  <div class="box">
    <h2>先绑定这台手机</h2>
    <ol>
      <li>电脑端便签右上角点 <b>⚙</b> →「账户与设备绑定」</li>
      <li>点 <b>生成绑定码（手机扫码用）</b></li>
      <li>用相机扫那个二维码，或把码抄下来填到下面（也可以直接打开二维码里那条链接，本页会自动填好）</li>
    </ol>
    <label class="fld">绑定码<input id="bind-code" placeholder="AB3D-7F9K" autocomplete="off"></label>
    <div class="row">
      <button class="primary" id="bind-go">绑定</button>
      <button id="phone-code">生成我的绑定码（给电脑端输）</button>
    </div>
    <div id="mycode"></div>
    <p class="hint" id="bind-status">没绑定之前，任务列表不会同步到这台手机。</p>
  </div>
</div>

<div id="app">
<div id="add">
  <input id="text" placeholder="明天下午三点开会   （直接写时间）" autocomplete="off">
  <button id="send">加</button>
</div>

<main id="list"><div id="empty">电脑端便签上的任务会出现在这里。</div></main>
</div>

<footer>
  演示用回环: 这台"手机"与电脑端直连, 没有公网服务器、没有账号体系, 变更靠局域网实时推送。<br>
  绑定是真的: 用电脑端签发的绑定码绑定, 成功后拿到设备令牌；电脑端撤销后这台手机要重新绑定。<br>
  正式版: 云端唯一事实源 + 手机本地通知 + 厂商推送唤醒 (见 docs/云端同步与数据模型设计.md)。
</footer>

<div id="sheet">
  <div class="sheet-box">
    <h2 id="sheet-h">事件属性</h2>
    <label class="fld">名称<input id="f-title" autocomplete="off"></label>
    <label class="fld" id="row-time">事件时间<input id="f-time" type="datetime-local"></label>
    <label class="fld" id="row-level">重要程度<select id="f-level"></select></label>
    <label class="fld" id="row-dur">持续时间(分钟)<input id="f-dur" type="number" min="0" max="1440" inputmode="numeric"></label>
    <label class="fld" id="row-note">备注<input id="f-note" autocomplete="off"></label>
    <p class="hint" id="sheet-hint"></p>
    <div class="row">
      <button class="primary" id="sheet-ok">保存</button>
      <button id="sheet-cancel">取消</button>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const WS_PORT = __WS_PORT__;
let ws, tasks = {}, seq = 0, levels = ["日常", "重要", "紧急"];
let deviceToken = null;
try { deviceToken = localStorage.getItem("hr_device_token"); } catch (e) { deviceToken = null; }

// ---- 绑定闸门: 没绑定就只显示绑定卡 (任务列表不推过来) ----------------------
function byId(id) { return document.getElementById(id); }
function showGate(text) {
  byId("gate").style.display = "block";
  byId("app").style.display = "none";
  if (text) byId("bind-status").textContent = text;
}
function showApp() {
  byId("gate").style.display = "none";
  byId("app").style.display = "block";
  render();
}
function saveToken(t) {
  deviceToken = t || null;
  try { if (t) localStorage.setItem("hr_device_token", t); else localStorage.removeItem("hr_device_token"); }
  catch (e) { /* 隐私模式下存不了: 下次重连要重新输码, 不影响本次绑定 */ }
}
byId("bind-go").onclick = () => {
  const code = byId("bind-code").value.trim();
  if (!code) { byId("bind-status").textContent = "先填绑定码（电脑端点「生成绑定码」拿到的那串）"; return; }
  byId("bind-status").textContent = "正在绑定…";
  send({ t: "op", op: "bind", code: code, name: "手机端(网页演示)" });
};
byId("phone-code").onclick = () => {
  byId("bind-status").textContent = "正在生成…";
  send({ t: "op", op: "phone_code" });
};
byId("bind-code").addEventListener("keydown", e => { if (e.key === "Enter") byId("bind-go").click(); });
// 扫码/点链接进来: ?code=XXXX 等价于扫到了码
try {
  const q = new URLSearchParams(location.search);
  if (q.get("code")) { byId("bind-code").value = q.get("code"); }
} catch (e) { /* 老浏览器不支持 URLSearchParams 也不影响手输 */ }

function fmt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const p = n => String(n).padStart(2, "0");
  return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c])); }
function toast(t) { const el = document.getElementById("toast"); el.textContent = t; el.className = "show";
  clearTimeout(el._h); el._h = setTimeout(() => el.className = "", 2200); }
function setStat(on, text) {
  document.getElementById("dot").className = "dot" + (on ? " on" : "");
  document.getElementById("state").textContent = text;
  document.getElementById("seq").textContent = seq;
  document.getElementById("n").textContent = Object.keys(tasks).length;
}
function byNext(a, b) { return (a.next_fire || "~") < (b.next_fire || "~") ? -1 : 1; }
function ordered() {
  const all = Object.keys(tasks).map(u => tasks[u]);
  const parents = all.filter(t => !t.parent_uuid).sort(byNext);
  const out = [];
  const used = {};
  parents.forEach(p => {
    out.push(p); used[p.uuid] = 1;
    all.filter(t => t.parent_uuid === p.uuid).sort(byNext)
       .forEach(k => { out.push(k); used[k.uuid] = 1; });
  });
  // 父任务已经不在列表里的子任务(另一端删了主任务)也要显示, 否则它还在响却看不见
  all.filter(t => !used[t.uuid]).sort(byNext).forEach(t => out.push(t));
  return out;
}
function render() {
  const list = document.getElementById("list");
  const arr = ordered();
  if (!arr.length) { list.innerHTML = '<div id="empty">电脑端便签上的任务会出现在这里。</div>'; setStat(ws && ws.readyState === 1, stat_text); return; }
  list.innerHTML = arr.map(t => {
    const kids = t.child_count ? ` · <span class="children">${t.child_count} 个子任务</span>` : "";
    const dur = t.duration_min ? ` · 持续 ${t.duration_min} 分钟` : "";
    const note = t.note ? `<div class="note">备注：${esc(t.note)}</div>` : "";
    return `<div class="card${t.status !== "active" ? " done" : ""}${t.depth ? " child" : ""}">
      <div class="title">${t.depth ? "└ " : ""}${esc(t.title)}</div>
      <div class="meta">[${esc(t.level)}] 事件 ${fmt(t.event_at)} · 下次提醒 <b>${fmt(t.next_fire)}</b>
        · 已响 ${t.fired}/${t.total}${dur}${kids} · v${t.version}</div>
      ${note}
      <div class="row">
        <button class="primary" onclick="op('complete','${t.uuid}')">完成</button>
        <button onclick="op('snooze','${t.uuid}')">稍后5分</button>
        <button onclick="openAttr('${t.uuid}')">属性</button>
      </div>
      <div class="row">
        ${t.depth ? "" : `<button onclick="openChild('${t.uuid}')">＋子任务</button>`}
        <button class="danger" onclick="op('delete','${t.uuid}')">删除</button>
      </div></div>`;
  }).join("");
  setStat(ws && ws.readyState === 1, stat_text);
}
let stat_text = "连接中…";
function op(o, u) { send({ t: "op", op: o, uuid: u }); }
function send(m) {
  if (!ws || ws.readyState !== 1) { toast("还没连上电脑端"); return; }
  ws.send(JSON.stringify(m));
}
function connect() {
  ws = new WebSocket("ws://" + location.hostname + ":" + WS_PORT + "/");
  ws.onopen = () => { stat_text = "已连上电脑端"; };
  ws.onclose = () => { stat_text = "断开, 2 秒后重连"; setStat(false, stat_text);
                       setTimeout(connect, 2000); };
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.t === "hello") {                      // 服务端要求先绑定
      showGate(deviceToken ? "正在用上次的绑定恢复…" : "");
      if (deviceToken) { send({ t: "op", op: "resume", token: deviceToken }); }
      setStat(true, deviceToken ? "恢复绑定中…" : "未绑定");
    }
    else if (m.t === "bound") {                 // 绑定成功 (或凭令牌恢复成功)
      if (m.device && m.device.token) saveToken(m.device.token);
      showApp();
      stat_text = "已绑定 · 已连上电脑端";
      render();
    }
    else if (m.t === "pair_code") {              // 手机端自己生成的码, 给电脑端输
      byId("mycode").textContent = m.code;
      byId("bind-status").textContent =
        "把这串码填到电脑端：⚙ →「账户与设备绑定」→ 输入框 → 绑定（" +
        Math.round((m.ttl_s || 300) / 60) + " 分钟内有效）";
    }
    else if (m.t === "snapshot") { tasks = {}; if (m.levels && m.levels.length) levels = m.levels;
      m.tasks.forEach(t => tasks[t.uuid] = t); seq = m.seq; showApp(); }
    else if (m.t === "delta") { seq = m.seq;
      if (m.ev === "remove") delete tasks[m.task.uuid]; else tasks[m.task.uuid] = m.task; render(); }
    else if (m.t === "notice") { toast(m.text);
      if (m.text && m.text.indexOf("失效") >= 0) { saveToken(null); showGate(m.text); } }
  };
}
document.getElementById("send").onclick = () => {
  const el = document.getElementById("text");
  const v = el.value.trim();
  if (!v) { toast("先写点什么"); return; }
  send({ t: "op", op: "add", text: v });
  el.value = "";
};
document.getElementById("text").addEventListener("keydown", e => {
  if (e.key === "Enter") document.getElementById("send").click();
});

// ---- 事件属性 / 建子任务: 共用一个底部表单 --------------------------------
let sheetMode = null, sheetUuid = null;
function localInput(iso) {
  if (!iso) return "";
  const d = new Date(iso), p = n => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
    + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
}
function fillLevels(sel, cur) {
  sel.innerHTML = levels.map(l => "<option>" + esc(l) + "</option>").join("");
  sel.value = cur || levels[0];
}
function showSheet(mode, uuid, heading) {
  const t = tasks[uuid] || {}, attr = mode === "attr";
  sheetMode = mode; sheetUuid = uuid;
  byId("sheet-h").textContent = heading;
  ["row-time", "row-level", "row-note"].forEach(id => { byId(id).style.display = attr ? "block" : "none"; });
  if (attr) {
    byId("f-title").value = t.title || "";
    byId("f-time").value = localInput(t.event_at);
    fillLevels(byId("f-level"), t.level);
    byId("f-dur").value = t.duration_min || 0;
    byId("f-note").value = t.note || "";
    byId("sheet-hint").textContent =
      "改「事件时间」后电脑端会按新时间重排提醒；改主任务时间，它下面的子任务整条跟着平移。";
  } else {
    byId("f-title").value = "";
    byId("f-dur").value = 30;
    byId("sheet-hint").textContent =
      "开始时间不用填：按「上一个子任务的开始 + 它的持续时间」自动顺推。每条子任务都是真提醒，到点照样响。";
  }
  byId("sheet").className = "show";
  byId("f-title").focus();
}
function openAttr(u) { showSheet("attr", u, "事件属性"); }
function openChild(u) { showSheet("child", u, "在「" + ((tasks[u] || {}).title || "") + "」下加子任务"); }
function closeSheet() { byId("sheet").className = ""; sheetMode = null; sheetUuid = null; }
byId("sheet-cancel").onclick = closeSheet;
byId("sheet").onclick = e => { if (e.target.id === "sheet") closeSheet(); };
byId("sheet-ok").onclick = () => {
  const title = byId("f-title").value.trim();
  if (!title) { toast("先填名称"); return; }
  const dur = parseInt(byId("f-dur").value || "0", 10) || 0;
  if (sheetMode === "attr") {
    send({ t: "op", op: "attr", uuid: sheetUuid, fields: {
      title: title, event_at: byId("f-time").value, level: byId("f-level").value,
      duration_min: dur, note: byId("f-note").value.trim() } });
  } else {
    send({ t: "op", op: "add_child", uuid: sheetUuid, text: title, duration_min: dur });
  }
  closeSheet();
};
connect();
</script>
</body>
</html>
"""
