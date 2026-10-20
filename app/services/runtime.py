"""任务运行时：让耗时的大模型调用不阻塞 HTTP 请求。

为什么不用 Celery / Redis：
    单人自用、单机部署，为了一个后台任务引入 broker 与额外进程并不划算。
    但接口按「任务可异步创建 + 可查询 + 可订阅」设计，
    将来换成 Celery 或换框架时，路由层不需要改。

线程模型：
    请求线程：创建 Turn 行 → 提交到线程池 → 立即返回 turn_id
    工作线程：跑大模型 → 逐段 publish(delta) → 完成后落库、广播 done
    订阅线程：SSE 端点从 attach() 拿到的队列里取事件推给浏览器

## 这一版修的是"事件会漏"

旧实现的做法是：SSE 端点在**请求线程**里先读一次 `Turn` 快照，然后在生成器里
才 `subscribe()`。这两步之间有一个窗口——如果工作线程正好在窗口内完成，
`done` 事件会广播给"当时还不存在的订阅者"，于是前端永远等不到终态：

    读快照 → 返回 running
                 （工作线程：保存 done + 广播 done ← 此时没有订阅者）
    建立订阅 → 队列是空的 → 一直挂着

现在改成 **`attach()` 一次性完成"快照 + 登记订阅"**，且与 `publish()` 共用同一把锁：
要么订阅者先登记（之后的每个事件都收得到），要么终态已经记下（直接补发）。
中间没有第三个状态，所以**不可能漏掉终态**。

除此之外还顺带解决四件事（都在这份文件里，因为它们是同一个"任务可靠性"问题）：

    ① **终态可补发**：`_terminal` 记住最后一个 done/error，连晚了也能拿到；
    ② **中间输出可回放**：`delta` 文本在内存里累加，连晚了能补上"目前为止的全文"
       （只保留非 delta 事件与累加后的全文，不逐条留存增量，避免内存无界）；
    ③ **提交失败不再留 pending**：线程池拒绝任务时把 Turn 明确标成 failed；
    ④ **可取消**：`cancel()` 之后 worker 下一次 `publish()` 会抛 `Cancelled`，
       流式输出因此会自然中止，而不是跑到一半还在写库。

**边界说清楚**：这些都在**进程内**。进程重启后内存里的缓冲与终态都会消失，
那时靠 `GET /api/turns/<id>` 从数据库读最终结果（`Turn.result`）恢复——
这也是 `turns.py` 里"终态优先从数据库回答"的原因。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from ..extensions import db
from ..models import Turn

log = logging.getLogger(__name__)

#: 每个任务在内存里保留多少条**非增量**事件用于回放（status/diagram/sources…）。
#: 增量（delta）不逐条保留，只累加成一份全文，见 `_delta_text`。
EVENTS_KEPT = 300

#: 最多保留多少个**已结束**任务的缓冲。长跑的进程里如果不设上限，
#: 每门课每轮都留下几百条事件，内存会一直涨。
FINISHED_KEPT = 60

#: 派生工作（回合后的复盘与观察）用的**独立**线程池容量。
#:
#: 为什么不和主任务共用同一个池：主池的 2 个位置是给"用户正等着的"任务的。
#: 如果复盘也挤进去，两轮连续讲课时复盘会把第三个请求排到后面，
#: 用户看到的是"点了没反应"。分开之后两者各自有界——**有界**是关键，
#: 不是"共用"：
#:     · 主池  max_workers（默认 2）—— 讲 / 备课 / 规划 / 图解
#:     · 侧池  1 —— 回合后的复盘与观察（同一时刻只跑一件，天然串行）
SIDE_WORKERS = 1

#: 下一轮教学最多等上一轮复盘多久（秒）。
#:
#: 为什么要有这个等待：复盘会更新 `LessonPlan.current_step`，而下一轮讲课
#: 读的正是它。不等的话，用户"答完立刻说继续"时，下一轮会用**旧进度**开讲，
#: 表现为同一批内容被重复讲一遍。等待是**有上限**的：复盘慢到 20 秒以上时
#: 宁可带着旧进度开讲，也不能把课卡住。
SIDE_WAIT_SECONDS = 20.0

#: 一个任务最多跑多久（秒）。默认 10 分钟。
#:
#: **为什么需要它**：模型接口偶尔会挂住（连接建立后不返回数据）。线程池里的
#: 线程没法被强行杀掉，于是那条 "正在讲" 的记录会永远停在 running，
#: 用户只能重启程序——而重启又会把这一轮标成"被重启打断"。
#: 有了超时，到点之后任务会被明确收尾（发 error、状态落库），用户能重试。
#:
#: 为什么取 10 分钟：一份 300 页的资料生成大纲可能确实要几分钟。
#: 这个值可以按机器调（`.env` 的 `TURN_TIMEOUT_SECONDS`）。
TURN_TIMEOUT_SECONDS = float(os.getenv("TURN_TIMEOUT_SECONDS", "600") or 600)

#: 一轮任务"多久没有任何事件"就算停滞（秒）。按任务类型覆盖，见 `api/_common.py`。
#:
#: 为什么需要它，和"总超时"有什么区别：
#:     总超时管的是"这件事整体要多久"（讲一轮最多 15 分钟）；
#:     停滞管的是"**它还在动吗**"——流式生成停在半路时，总超时还没到，
#:     但界面上已经几十秒没有新字了。用户看到的就是"一直讲课中、永远不结束"。
#: 两者的收尾动作一样（落库 failed + 发 error），只是判据不同。
TURN_STALL_SECONDS = float(os.getenv("TURN_STALL_SECONDS", "300") or 300)

#: 看门狗扫描间隔（秒）。进程内**只有一个**看门狗线程（见 `_ensure_watchdog`），
#: 不是每个应用实例一个——测试里会创建几百个 app，一个实例一个线程会耗尽线程数。
WATCHDOG_INTERVAL_SECONDS = 5.0

#: 所有活着的运行时。看门狗只扫这些。
_RUNTIMES: "weakref.WeakSet[TurnRuntime]" = weakref.WeakSet()
_watchdog_lock = threading.Lock()
_watchdog_thread: threading.Thread | None = None


def _ensure_watchdog() -> None:
    """确保进程内有一个看门狗线程在跑。"""
    global _watchdog_thread
    with _watchdog_lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop, name="turn-watchdog", daemon=True
        )
        _watchdog_thread.start()


def _watchdog_loop() -> None:
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        for runtime in list(_RUNTIMES):
            try:
                runtime.sweep_timeouts()
                runtime.sweep_stalls()
            except Exception:  # noqa: BLE001 —— 看门狗自己绝不能挂
                log.exception("超时/停滞扫描失败")

#: 终态。只有这三种算"这一轮已经有结论了"。
TERMINAL_STATUSES = ("done", "failed", "cancelled")

#: 会被当作"结束信号"的事件类型。
TERMINAL_EVENTS = ("done", "error")

#: 当前线程正在执行哪个 Turn。
#:
#: 为什么需要它：`Turn` 行是在 **worker 被构造之后**才创建的（`start_turn` 先建行、
#: 再 submit），所以 worker 闭包里拿不到自己的 turn_id，授课消息因此一直
#: 写的是 `turn_id=None`——评审 5.8 说的"教学过程不可追踪"就是从这一环断的。
#: 现在由运行时在调用 worker 之前把 id 放进线程本地，业务代码用
#: `current_turn_id()` 就能拿到，不需要改所有 worker 的签名。
_thread_state = threading.local()


def current_turn_id() -> int | None:
    """当前线程正在执行的 Turn id；不在 worker 里时返回 None。"""
    return getattr(_thread_state, "turn_id", None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Cancelled(Exception):
    """worker 内部的中止信号。

    由 `publish()` 在收到取消请求时抛出。**故意用异常而不是返回值**：
    worker 的签名是"给一个 publish 回调、返回最终文本"，中间没有地方检查返回值；
    而 `publish()` 在流式输出里被反复调用（每段文字一次），在那里抛异常
    就能让 worker 在任何一次产出处立刻停下——不需要给每个 worker 加取消检查。
    """


class Attachment:
    """一次订阅的结果：`attach()` 的返回值。

    `queue` 为 `None` 表示"不需要再等"——要么已经有终态可补发，
    要么这个任务在运行时里已经没有任何记录（进程重启过）。
    """

    __slots__ = ("buffered", "terminal", "queue")

    def __init__(self, buffered: list[dict], terminal: dict | None,
                 live: queue.Queue | None) -> None:
        self.buffered = buffered
        self.terminal = terminal
        self.queue = live

    @property
    def finished(self) -> bool:
        """已经有终态了吗？有的话调用方不该再等队列。"""
        return self.terminal is not None


class TurnRuntime:
    def __init__(self, max_workers: int = 2) -> None:
        # 并发上限同时是 API 成本护栏：避免同时打出去太多请求
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="turn"
        )
        # 在线订阅者
        self._subs: dict[int, list[queue.Queue]] = {}
        # 每个任务的非增量事件（回放用）
        self._events: dict[int, deque] = {}
        # 每个任务的增量全文（回放用，只留累加结果）
        self._delta_text: dict[int, list[str]] = {}
        # 每个任务的终态事件（补发用）
        self._terminal: dict[int, dict] = {}
        # 事件序号（诊断用；也方便将来做"只补发 seq > n"）
        self._seq: dict[int, int] = {}
        # 收到过取消请求的任务
        self._cancelled: set[int] = set()
        # 取消原因（给用户看的那句话）：超时与用户主动取消要分得开
        self._cancel_reason: dict[int, str] = {}
        # 每个任务的截止时间（monotonic 秒）与它自己的超时值
        self._deadlines: dict[int, float] = {}
        self._timeout_values: dict[int, float] = {}
        # 每个任务"最后一次有事件"的时刻（monotonic）。停滞判定看它。
        self._last_event: dict[int, float] = {}
        # 每个任务的停滞阈值（秒）；没给就用 TURN_STALL_SECONDS
        self._stalls: dict[int, float] = {}
        # 会话级互斥：同一会话同一时刻只让一个"会写教案/消息"的任务在跑。
        # 键是 session_id。用 Lock 而不是信号量：这里的语义是"互斥"，不是"限流"。
        self._session_locks: dict[int, threading.Lock] = {}
        # 派生工作（复盘/观察）的登记：谁在做、做完没。
        # `_side_pending` 是计数（同一个会话理论上可能排了两件），
        # `_side_idle` 是"清零了就 set"的闸口，供 `wait_for_side()` 等待。
        self._side_pending: dict[int, int] = {}
        self._side_idle: dict[int, threading.Event] = {}
        # 派生工作用的独立线程池：容量 1，天然串行（见 SIDE_WORKERS 的说明）
        self._side_executor = ThreadPoolExecutor(
            max_workers=SIDE_WORKERS, thread_name_prefix="side"
        )
        # 一把锁保护上面全部结构：**attach 与 publish 必须互斥**，
        # 否则"登记订阅"和"广播终态"之间又会出现一次漏接的窗口。
        self._lock = threading.Lock()

        # 注册到进程级的看门狗（只创建一个线程，见 _ensure_watchdog）
        _RUNTIMES.add(self)
        _ensure_watchdog()
        # 看门狗线程不在请求里，也就没有 app context——而超时收尾要访问数据库。
        # 所以在第一次 `submit()` 时把 app 记下来，收尾时自己压一个上下文。
        self._app = None

    # ---------------- 订阅 ----------------

    def attach(self, turn_id: int) -> Attachment:
        """**原子地**取快照并登记订阅。

        调用方拿到返回值后：先发 `buffered` 里的事件，若 `terminal` 非空则发完就结束，
        否则从 `queue` 里继续取实时事件。这三步之间不会漏事件——因为从
        `attach()` 返回的那一刻起，后续每个 `publish()` 都会投进这个队列。
        """
        with self._lock:
            buffered = list(self._events.get(turn_id, ()))
            text_parts = self._delta_text.get(turn_id)
            terminal = self._terminal.get(turn_id)
            live: queue.Queue | None = None

            if text_parts:
                # 已经产出过的文字：合成一条 delta 补发。
                # 为什么合成而不是逐条留存：逐条留存会让内存随输出长度线性增长
                # （一次讲课可以产生上千条小增量），而回放需要的只是"目前为止的全文"。
                buffered.append({"type": "delta", "text": "".join(text_parts)})

            if terminal is None:
                live = queue.Queue()
                self._subs.setdefault(turn_id, []).append(live)
            elif terminal not in buffered:
                # 终态被 `_prune` 之外的路径漏进缓冲时兜一下（正常情况下
                # `_emit` 已经把它写进 `_events`，这里只是保证"一定有"）。
                buffered.append(terminal)

            return Attachment(buffered, terminal, live)

    def subscribe(self, turn_id: int) -> queue.Queue:
        """兼容旧接口：只要队列的话用它。**新代码请用 `attach()`**。

        它单独用会有"登记之前的事件收不到"的问题——`attach()` 就是为这件事存在的。
        """
        attachment = self.attach(turn_id)
        if attachment.queue is None:
            # 已有终态：给一个"已经结束"的队列，调用方按顺序发完 buffered 即可。
            done_queue: queue.Queue = queue.Queue()
            done_queue.put(None)
            return done_queue
        return attachment.queue

    def unsubscribe(self, turn_id: int, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(turn_id)
            if subs and q in subs:
                subs.remove(q)
            if not subs:
                self._subs.pop(turn_id, None)

    def close(self, turn_id: int) -> None:
        """给所有订阅者投递结束信号（None 是哨兵）。

        主要作用是**唤醒**卡在 `get(timeout=...)` 上的订阅线程，
        免得它要等到下一次心跳才发现任务已经结束。
        """
        with self._lock:
            subs = list(self._subs.get(turn_id, ()))
        for q in subs:
            q.put(None)

    # ---------------- 广播 ----------------

    def publish(self, turn_id: int, event: dict[str, Any]) -> None:
        """工作线程往外发事件。**被取消、超时、或任务已结束时都会抛 `Cancelled`。**

        为什么"任务已结束"也要中止：超时是**看门狗**收尾的，它会顺手清掉取消标记。
        于是卡住之后又醒过来的 worker 再去 publish 时，`is_cancelled` 已经是 false，
        它会继续往一个**已经发出 error** 的任务里塞增量——前端收到的就是
        "报错之后又冒出半段文字"。所以终态本身就是中止信号。
        （这条是 `test_publish_after_timeout_raises` 偶发失败时才暴露的。）
        """
        if self.is_cancelled(turn_id) or self.is_expired(turn_id):
            if self.is_expired(turn_id) and not self.is_cancelled(turn_id):
                # 超时由看门狗负责收尾；这里只负责让 worker 停下来
                self.cancel(turn_id, reason=self._timeout_reason())
            raise Cancelled(self.cancel_reason(turn_id))

        if self.is_terminal(turn_id):
            raise Cancelled(self.cancel_reason(turn_id) or "任务已结束")

        self._emit(turn_id, event)

    def is_terminal(self, turn_id: int) -> bool:
        """这个任务已经有终态（done / error）了吗？"""
        with self._lock:
            return turn_id in self._terminal

    def _emit(self, turn_id: int, event: dict[str, Any]) -> None:
        """真正写入缓冲 + 广播。终态也走这条路（所以取消之后仍能发出 error）。"""
        etype = event.get("type")

        # 阶段文字要落库，不能只随 SSE 走掉：
        # 用户完全可能在这几分钟里离开页面，回来时问"跑到哪一步了"——
        # 那时只能从数据库回答，而那条 SSE 早就断了。
        # 这一步有数据库 I/O，所以放在锁外面（不让它拖住订阅登记）。
        if etype == "status" and event.get("status"):
            self._remember_stage(turn_id, str(event["status"]))

        with self._lock:
            seq = self._seq.get(turn_id, 0) + 1
            self._seq[turn_id] = seq
            stamped = {**event, "seq": seq}
            # 停滞判定看的是"多久没有事件"，所以每个事件都要刷新时间戳
            self._last_event[turn_id] = time.monotonic()

            if etype == "delta":
                # 增量：只累加文本，不进事件缓冲（否则内存随输出线性增长）
                self._delta_text.setdefault(turn_id, []).append(str(event.get("text", "")))
            else:
                self._events.setdefault(turn_id, deque(maxlen=EVENTS_KEPT)).append(stamped)

            if etype in TERMINAL_EVENTS:
                self._terminal[turn_id] = stamped

            subs = list(self._subs.get(turn_id, ()))

        for q in subs:
            q.put(stamped)

        # 终态已经写进 `_terminal` 了：这时才能回收旧缓冲。
        # **这里必须放在 `_emit` 里、而不是 `_finish` 里**——因为
        # `_finish` 只提交数据库，`_terminal` 是在本函数里才被写入的。
        # 最初把 `_prune()` 放在 `_finish` 中，于是"回收"永远发生在
        # "新增"之前，稳定状态是上限 +1（有测试抓到了这一点）。
        if etype in TERMINAL_EVENTS:
            self.clear_cancel(turn_id)
            self._prune()

    @staticmethod
    def _remember_stage(turn_id: int, stage: str) -> None:
        """把当前阶段写回 Turn 行。

        **只在变化时写**：阶段文字在一次任务里会重复出现，每次都 commit
        是白白的写放大。这里也**绝不让失败影响主流程**——记录进度是附加价值，
        它出错不该把一次正常的规划带崩。
        """
        try:
            turn = db.session.get(Turn, turn_id)
            if turn is None or turn.stage == stage:
                return
            turn.stage = stage[:200]
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()

    # ---------------- 取消 ----------------

    def cancel(self, turn_id: int, *, reason: str = "任务已取消") -> None:
        """请求取消。

        **是"请求"不是"立即停止"**：worker 下一次 `publish()` 才会抛 `Cancelled`。
        对"每段输出都 publish 一次"的流式任务，这个延迟是几十毫秒；
        对一整段不 publish 的模型调用（比如规划），要等那次调用返回。
        这是刻意的：进程内没有安全的办法中断一个正在跑的 HTTP 请求。

        `reason` 会出现在最终的错误里，所以"用户点的取消"和"超时"要分开传——
        用户看到"任务超时"会去查资料/换模型，看到"已取消"只会重试。
        """
        with self._lock:
            self._cancelled.add(turn_id)
            self._cancel_reason.setdefault(turn_id, reason)

    def cancel_reason(self, turn_id: int) -> str:
        with self._lock:
            return self._cancel_reason.get(turn_id, "任务已取消")

    def is_cancelled(self, turn_id: int) -> bool:
        with self._lock:
            return turn_id in self._cancelled

    def clear_cancel(self, turn_id: int) -> None:
        with self._lock:
            self._cancelled.discard(turn_id)
            self._cancel_reason.pop(turn_id, None)
            self._deadlines.pop(turn_id, None)

    # ---------------- 超时 ----------------

    @staticmethod
    def _timeout_reason(seconds: float | None = None) -> str:
        total = int(seconds if seconds is not None else TURN_TIMEOUT_SECONDS)
        return f"任务超时（超过 {total} 秒没有结果），已中止"

    def is_expired(self, turn_id: int) -> bool:
        with self._lock:
            deadline = self._deadlines.get(turn_id)
        return deadline is not None and time.monotonic() > deadline

    def sweep_timeouts(self) -> None:
        """把已经超时的任务收尾。由看门狗定期调用，也可以在测试里直接调。

        **谁来收尾很重要**：如果只让 worker 自己发现超时，那么"卡在一次
        永不返回的模型调用里"的 worker 永远不会走到下一次 publish，
        界面就永远停在"正在讲"。所以这里主动落库并广播 error——
        用户能立刻重试，而不必重启程序。
        """
        now = time.monotonic()
        with self._lock:
            expired = [
                (turn_id, self._timeout_values.get(turn_id))
                for turn_id, deadline in self._deadlines.items()
                if now > deadline and turn_id not in self._terminal
            ]

        for turn_id, timeout_value in expired:
            reason = self._timeout_reason(timeout_value)
            self.cancel(turn_id, reason=reason)
            # 看门狗线程没有请求上下文，这里自己压一个；没有 app 就只发事件不落库
            # （那种情况只可能出现在"提交了但还没记录 app"的极端时序里）。
            app = self._app
            if app is not None:
                try:
                    with app.app_context():
                        self._finish_open(turn_id, "failed", error=reason)
                except Exception:  # noqa: BLE001
                    log.exception("超时收尾失败（任务 %s）", turn_id)
            self._emit(turn_id, {"type": "error", "message": reason})
            self.close(turn_id)
            log.warning("任务 %s 超时，已中止", turn_id)

    def sweep_stalls(self) -> None:
        """把"还有很久没有动静"的任务收尾。

        与 `sweep_timeouts` 的区别：
            总超时管"这件事整体最多多久"；停滞管"**它现在还在动吗**"。
            流式生成停在半路时，总超时往往还没到，但界面上已经几十秒没有新字——
            用户看到的就是"一直讲课中、永远不结束"（真实反馈）。
        阈值按任务类型给（见 `api/_common.py` 的 `TURN_STALLS`）：
        讲授一直在吐字，90 秒没动静就是死了；出大纲可能几分钟没有中间事件，
        用同一个阈值会把正常的任务误杀。
        """
        now = time.monotonic()
        with self._lock:
            stalled = [
                (turn_id, self._stalls.get(turn_id) or TURN_STALL_SECONDS)
                for turn_id, last in self._last_event.items()
                if turn_id not in self._terminal
                and turn_id in self._deadlines          # 只算"提交过、还在跑"的
                and now - last > (self._stalls.get(turn_id) or TURN_STALL_SECONDS)
            ]

        for turn_id, limit in stalled:
            reason = f"生成停滞（已经 {int(limit)} 秒没有新内容），已中止"
            self.cancel(turn_id, reason=reason)
            app = self._app
            if app is not None:
                try:
                    with app.app_context():
                        self._finish_open(turn_id, "failed", error=reason)
                except Exception:  # noqa: BLE001
                    log.exception("停滞收尾失败（任务 %s）", turn_id)
            self._emit(turn_id, {"type": "error", "message": reason})
            self.close(turn_id)
            log.warning("任务 %s 停滞，已中止", turn_id)

    # ---------------- 会话互斥与派生工作 ----------------

    def session_lock(self, session_id: int | None) -> threading.Lock:
        """拿到某个会话的互斥锁。

        `session_id` 为 `None` 时返回一把**一次性**的锁（不共享）——
        那种情况是"没有会话可争"（比如一次纯规划），不该变成全局串行。
        """
        if session_id is None:
            return threading.Lock()
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def submit_side(self, app, fn: Callable[[], None], *, scope: int | None,
                    label: str = "side") -> bool:
        """把"派生工作"（回合后复盘/观察）交给有界的侧线程池。

        **为什么要经过运行时，而不是直接 `threading.Thread(...)`**：
        原来的实现每轮教学都新开一个 daemon 线程，而线程池的 2 个位置
        完全管不到它们。用户快速连发几轮，就会同时有好几个复盘线程
        在调模型、改教案、写 SQLite——线程数无界，且彼此并发写同一份教案。

        `scope`（这里是 session_id）用来做两件事：
            ① 让 `wait_for_side()` 能问"这个会话还有没有复盘没做完"；
            ② 让下一轮教学在开讲前等它一下（见 `SIDE_WAIT_SECONDS`）。

        提交失败不会抛：复盘是附加价值，排不进去只记日志。
        返回是否真的排上了。
        """
        self._begin_side(scope)

        def run() -> None:
            try:
                with app.app_context():
                    fn()
            except Exception:  # noqa: BLE001 —— 派生工作失败不该影响任何人
                log.exception("派生工作失败：%s", label)
            finally:
                self._end_side(scope)

        try:
            self._side_executor.submit(run)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("派生工作排不进去（%s）：%s", label, exc)
            self._end_side(scope)
            return False

    def _begin_side(self, scope: int | None) -> None:
        if scope is None:
            return
        with self._lock:
            self._side_pending[scope] = self._side_pending.get(scope, 0) + 1
            self._side_idle.setdefault(scope, threading.Event()).clear()

    def _end_side(self, scope: int | None) -> None:
        if scope is None:
            return
        with self._lock:
            remaining = self._side_pending.get(scope, 0) - 1
            if remaining <= 0:
                self._side_pending.pop(scope, None)
                event = self._side_idle.get(scope)
                if event is not None:
                    event.set()
            else:
                self._side_pending[scope] = remaining

    def wait_for_side(self, scope: int | None,
                      timeout: float = SIDE_WAIT_SECONDS) -> bool:
        """等这个会话的派生工作做完。返回是否等到（超时返回 False）。

        **必须在拿 `session_lock` 之前调用**：派生工作自己要拿那把锁，
        拿着锁等它就是死锁。
        """
        if scope is None:
            return True
        with self._lock:
            pending = self._side_pending.get(scope, 0)
            event = self._side_idle.setdefault(scope, threading.Event())
            if pending <= 0:
                return True
        return event.wait(timeout)

    def has_pending_side(self, scope: int | None) -> bool:
        if scope is None:
            return False
        with self._lock:
            return self._side_pending.get(scope, 0) > 0

    # ---------------- 执行 ----------------

    def submit(self, app, turn_id: int,
               worker: Callable[[Callable[[dict], None]], str],
               *, timeout: float | None = None,
               stall: float | None = None) -> None:
        """把 worker 丢进线程池。

        worker 收到一个 publish 回调用来推送增量事件，返回最终全文。
        `timeout` 是**总时长**上界（不给就用 `TURN_TIMEOUT_SECONDS`）；
        `stall` 是**多久没有事件**算停滞（不给就用 `TURN_STALL_SECONDS`）。
        两者都由看门狗收尾并广播 error，不会让界面永远停在"正在跑"。
        """
        limit = float(timeout if timeout is not None else TURN_TIMEOUT_SECONDS)
        stall_limit = float(stall if stall is not None else TURN_STALL_SECONDS)
        # 记住 app：超时/停滞都是看门狗收尾的，那条线程没有请求上下文
        self._app = app
        with self._lock:
            self._deadlines[turn_id] = time.monotonic() + limit
            self._timeout_values[turn_id] = limit
            # 基线：提交那一刻就算"刚有过动静"，否则排队等待执行会被误判成停滞
            self._last_event[turn_id] = time.monotonic()
            self._stalls[turn_id] = stall_limit

        def run() -> None:
            # 工作线程没有请求上下文，必须自己压一个 app context，
            # 否则 db.session 不可用。
            with app.app_context():
                # 让业务代码知道"我现在属于哪一轮"——授课消息靠它写 turn_id
                _thread_state.turn_id = turn_id
                try:
                    if self.is_cancelled(turn_id):
                        # 还没开始就已经被取消：不要白跑一次模型调用
                        self._finish(turn_id, "cancelled",
                                     error="任务在开始前被取消")
                        self._emit(turn_id, {"type": "error", "message": "任务已取消"})
                        return

                    self._mark_running(turn_id)
                    self._emit(turn_id, {"type": "status", "status": "running"})

                    try:
                        text = worker(lambda ev: self.publish(turn_id, ev))
                    except Cancelled:
                        # 取消/超时都可能走到这里。理由由 `cancel_reason` 决定，
                        # 并且**只在任务还没被收尾时**才落库：
                        # 超时是看门狗先收尾的，worker 晚一步醒来不能再改写状态，
                        # 也不能再补发一条 done——那会让前端收到两个终态。
                        reason = self.cancel_reason(turn_id)
                        status = "failed" if self.is_expired(turn_id) else "cancelled"
                        if self._finish_open(turn_id, status, error=reason):
                            self._emit(turn_id, {"type": "error", "message": reason})
                    except Exception as exc:  # noqa: BLE001 —— 要在前端显示任何失败原因
                        message = str(exc) or exc.__class__.__name__
                        if self._finish_open(turn_id, "failed", error=message):
                            self._emit(turn_id, {"type": "error", "message": message})
                    else:
                        if self._finish_open(turn_id, "done", result=text or ""):
                            self._emit(turn_id, {"type": "done", "text": text or ""})
                except Exception as exc:  # noqa: BLE001 —— 连落库都失败了：至少别静默
                    try:
                        self._finish_open(turn_id, "failed",
                                          error=f"任务运行时出错：{exc}")
                    except Exception:  # noqa: BLE001
                        db.session.rollback()
                finally:
                    self.close(turn_id)
                    _thread_state.turn_id = None
                    db.session.remove()

        try:
            self._executor.submit(run)
        except Exception as exc:  # noqa: BLE001
            # **提交失败必须落库**：Turn 行已经建好且是 pending，
            # 如果不改状态，它会永远显示"排队中"，而其实根本没跑。
            # 触发场景：线程池已 shutdown、系统线程耗尽（can't start new thread）。
            #
            # **不再往外抛**：调用方（`start_turn`）已经把 Turn 建好了，
            # 抛出去只会让接口返回 500，前端反而看不到"为什么没跑起来"。
            # 落库 + 发一条 error 事件之后，前端通过 SSE 或 `/api/turns/<id>`
            # 都能拿到确切原因。
            message = f"任务没能启动：{exc}"
            log.error("提交任务 %s 失败：%s", turn_id, exc)
            try:
                with app.app_context():
                    self._finish_open(turn_id, "failed", error=message)
            except Exception:  # noqa: BLE001
                pass
            self._emit(turn_id, {"type": "error", "message": message})
            self.close(turn_id)

    @staticmethod
    def _mark_running(turn_id: int) -> None:
        """把状态从 pending 改成 running。

        不这么做的话它会一直停在 pending 直到结束——而
        "排队中"和"正在跑"是两回事，`/api/turns/<id>` 报给前端的是前者。
        课程列表判断"在不在规划"时也依赖这个区别。
        """
        turn = db.session.get(Turn, turn_id)
        if turn is not None and turn.status == "pending":
            turn.status = "running"
            db.session.commit()

    def _finish(self, turn_id: int, status: str, *, result: str | None = None,
                error: str | None = None) -> None:
        """无条件写终态（内部用；外部收尾请用 `_finish_open`）。"""
        turn = db.session.get(Turn, turn_id)
        if turn is None:
            return
        turn.status = status
        if result is not None:
            turn.result = result
        if error:
            turn.error = error
        turn.finished_at = _now()
        db.session.commit()

    def _finish_open(self, turn_id: int, status: str, *, result: str | None = None,
                     error: str | None = None) -> bool:
        """只在任务**还没结束**时写终态，返回是否写了。

        为什么要"只在没结束时写"：超时是看门狗先收尾的，而 worker 可能
        在几秒后才从那次卡住的调用里醒来。如果它无条件再写一次，
        就会把"超时失败"覆盖成"已取消"（甚至"完成"），用户看到的状态
        跟他刚才看到的错误提示对不上。
        """
        turn = db.session.get(Turn, turn_id)
        if turn is None:
            return False
        if turn.status in TERMINAL_STATUSES:
            return False
        self._finish(turn_id, status, result=result, error=error)
        return True

    def _prune(self) -> None:
        """只保留最近若干个已结束任务的缓冲，避免内存无界增长。

        `dict` 在 Python 3.7+ 保序，所以"最早的"就是迭代顺序里的第一个。
        """
        with self._lock:
            while len(self._terminal) > FINISHED_KEPT:
                oldest = next(iter(self._terminal))
                self._terminal.pop(oldest, None)
                self._events.pop(oldest, None)
                self._delta_text.pop(oldest, None)
                self._seq.pop(oldest, None)
                self._subs.pop(oldest, None)
                self._cancelled.discard(oldest)
                self._cancel_reason.pop(oldest, None)
                self._deadlines.pop(oldest, None)
                self._timeout_values.pop(oldest, None)
                self._last_event.pop(oldest, None)
                self._stalls.pop(oldest, None)

    def shutdown(self, *, wait: bool = False) -> None:
        """停止线程池。**用于进程退出**，不做优雅等待（默认 `wait=False`）。

        为什么默认不等：窗口已经关了，用户要的是"程序退出"，而不是
        "再等几十秒让一个正在跑的模型调用结束"。`wait=False` 不会阻塞，
        残留的守护线程会随进程一起消失。
        """
        for executor in (self._executor, self._side_executor):
            try:
                executor.shutdown(wait=wait)
            except Exception:  # noqa: BLE001
                log.exception("关闭线程池失败")


def sse_event(event: dict[str, Any]) -> str:
    """把事件序列化成 SSE 帧。"""
    import json

    etype = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n"
