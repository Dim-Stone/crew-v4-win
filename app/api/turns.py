"""Turn 查询、订阅与取消接口。"""

from __future__ import annotations

import queue

from flask import Blueprint, Response, stream_with_context

from ..extensions import db
from ..models import Turn
from ..services.runtime import TERMINAL_STATUSES, sse_event
from ._common import fail, get_runtime, ok

bp = Blueprint("turns", __name__, url_prefix="/api/turns")

# SSE 心跳间隔（秒）：防止中间代理因长时间无数据而断开
KEEPALIVE_SECONDS = 20


@bp.get("/<int:turn_id>")
def get_turn(turn_id: int):
    """轮询兜底：SSE 断了也能拿到最终结果。

    它是前端断线恢复的**唯一依据**：连接一断，前端就来这里问"到底成功没成功"。
    所以返回里必须有 `status` 和 `result`/`error`——见 `Turn.to_dict()`。
    """
    turn = db.session.get(Turn, turn_id)
    if turn is None:
        return fail("任务不存在", 404)
    return ok(turn.to_dict())


@bp.get("/<int:turn_id>/stream")
def stream_turn(turn_id: int):
    """SSE：逐段推送任务进度与产出。

    **顺序很关键**（这一版修的就是它）：

        ① 先从数据库读一次快照——它只用于"进程重启过、内存里什么都没有"的情况；
        ② 若快照已是终态，直接发完就结束。**不订阅**——否则会去等一个
           永远不会再来的事件（进程重启后没有任何 worker 会发它）；
        ③ 否则 `runtime.attach()`：它**原子地**完成"补发缓冲 + 登记订阅"，
           所以从这一刻起不存在漏接终态的窗口。
    """
    turn = db.session.get(Turn, turn_id)
    if turn is None:
        return fail("任务不存在", 404)

    runtime = get_runtime()
    # 先把需要的数据读出来，避免在生成器里访问已过期的 ORM 对象
    snapshot = turn.to_dict()

    def generate():
        yield sse_event({
            "type": "snapshot",
            "status": snapshot["status"],
            "text": snapshot["result"] or "",
        })

        if snapshot["status"] == "done":
            yield sse_event({"type": "done", "text": snapshot["result"] or ""})
            return
        if snapshot["status"] in TERMINAL_STATUSES:
            yield sse_event({
                "type": "error",
                "message": snapshot["error"] or "任务已结束",
            })
            return

        attachment = runtime.attach(turn_id)
        try:
            # 缓冲里可能有 status / delta / diagram / …，末尾可能已经带终态
            for event in attachment.buffered:
                yield sse_event(event)
                if event.get("type") in ("done", "error"):
                    return

            if attachment.queue is None:
                # 没有活订阅（attach 时也没拿到终态）：极端情况下这里说明
                # 运行时里已经没有任何关于它的记录，只能如实收尾，
                # 让前端去 `/api/turns/<id>` 重查，而不是让它一直挂着。
                yield sse_event({
                    "type": "warn",
                    "message": "这个任务不在当前进程里，请刷新或重试",
                })
                return

            while True:
                try:
                    event = attachment.queue.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue

                if event is None:  # worker 结束的哨兵
                    break

                yield sse_event(event)
                if event.get("type") in ("done", "error"):
                    break
        finally:
            if attachment.queue is not None:
                runtime.unsubscribe(turn_id, attachment.queue)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"  # 让 nginx 之类不要缓冲
    response.headers["Connection"] = "keep-alive"
    return response


@bp.post("/<int:turn_id>/cancel")
def cancel_turn(turn_id: int):
    """请求取消一个还在跑的任务。

    **语义是"请求"**：worker 下一次产出（流式任务里就是下一段文字）时才会停下。
    已经结束的任务返回 409——不然用户会以为"点了取消但没生效"，
    而其实它早就做完了。
    """
    turn = db.session.get(Turn, turn_id)
    if turn is None:
        return fail("任务不存在", 404)
    if turn.status in TERMINAL_STATUSES:
        return fail("这个任务已经结束，不需要取消", 409)

    get_runtime().cancel(turn_id)
    return ok({"turn_id": turn_id, "status": "cancelling"})
