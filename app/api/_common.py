"""API 层公共工具。"""

from __future__ import annotations

from typing import Any, Callable

from flask import current_app, jsonify

from ..extensions import db
from ..models import Turn
from ..services.runtime import TurnRuntime


def get_runtime() -> TurnRuntime:
    return current_app.extensions["turn_runtime"]


def active_turn_for_session(session_id: int, kinds: tuple[str, ...]) -> Turn | None:
    """这个会话在跑的任务里，有没有这几种 kind 的。

    用途是**入口处的并发闸**：同一会话不允许同时跑两轮教学（否则两轮都读
    `LessonPlan.current_step`、都改 `covered`、都写 `Message`，进度会互相覆盖）。
    内部的互斥由 `runtime.session_lock()` 保证；这里是在更前面就拒绝，
    免得用户点两下、看到两份回复。
    """
    return (
        Turn.query.filter(
            Turn.session_id == session_id,
            Turn.status.in_(("pending", "running")),
            Turn.kind.in_(kinds),
        )
        .order_by(Turn.id.desc())
        .first()
    )


#: 每类任务的超时（秒）。超时后看门狗会把任务收尾并给出错误，
#: 而不是让界面永远停在"正在跑"。
#:
#: 为什么按 kind 分：一次图解不该等 10 分钟（它只需要一次结构化生成），
#: 而一份大资料生成大纲可能确实要十几分钟。用一个统一值，
#: 要么图解卡太久，要么大纲被误杀。
TURN_TIMEOUTS: dict[str, float] = {
    "teach": 900.0,      # 15 分钟：一轮讲授含备课 + 流式输出
    "outline": 1800.0,   # 30 分钟：资料模式要逐份读资料
    "plan": 1800.0,
    "intake": 300.0,     # 5 分钟：只是解析问卷
    "diagram": 300.0,    # 5 分钟：一次图解
    "quiz": 600.0,       # 10 分钟：出题 + 解析（重试一次）
}

#: 每类任务"多久没有任何事件"算停滞（秒）。
#:
#: 与 `TURN_TIMEOUTS` 是两把不同的尺子：总超时管"整体最多多久"，
#: 停滞管"**现在还在动吗**"。流式生成停在半路时，总超时往往还没到，
#: 而界面上已经几十秒没有新字——用户看到的就是"一直讲课中、永远不结束"。
#:
#: 讲授一路在吐字，90 秒没动静就是死了；出大纲/出题可能好几分钟没有中间事件，
#: 阈值必须放大，否则会把正常任务误杀。
TURN_STALLS: dict[str, float] = {
    # 讲授会先等上一轮复盘（复盘本身也是一次模型调用，最长 180s），
    # 所以阈值不能压得太低，否则"正常排队"会被判成卡死。
    "teach": 180.0,
    "quiz": 240.0,
    "outline": 600.0,
    "plan": 600.0,
    "intake": 120.0,
    "diagram": 150.0,
}


def start_turn(kind: str, worker: Callable[[Callable[[dict], None]], str],
               *, session_id: int | None = None, course_id: int | None = None,
               payload: dict | None = None,
               timeout: float | None = None) -> Turn:
    """创建 Turn 行并提交到线程池，立刻返回。

    这是全应用"不阻塞"的统一入口：任何耗时的大模型调用都必须走这里。

    `course_id` 是给大纲类任务用的：绑上它，课程列表才能回答
    "这门课现在有没有在跑的规划"。
    """
    import json

    turn = Turn(
        kind=kind,
        session_id=session_id,
        course_id=course_id,
        status="pending",
        payload=json.dumps(payload or {}, ensure_ascii=False),
    )
    db.session.add(turn)
    db.session.commit()

    # _get_current_object(): 线程池里要一个真正的 app 对象，而不是代理
    get_runtime().submit(
        current_app._get_current_object(),
        turn.id,
        worker,
        timeout=timeout if timeout is not None else TURN_TIMEOUTS.get(kind),
        stall=TURN_STALLS.get(kind),
    )
    return turn


def ok(data: Any = None, **extra):
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return jsonify(body)


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status
