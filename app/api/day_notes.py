"""某一天的"记事本"接口。

为什么与待办分成两个蓝图（虽然界面上它们挨着）：**失败后果不一样**——
待办写不进去是"清单少一条"，记事写不进去是"我刚写的一段话丢了"。
日志与前端提示因此分得开，而它们本来就该分开处理。
"""

from __future__ import annotations

from flask import Blueprint, request

from ..services import todo_service
from ._common import fail, ok

bp = Blueprint("day_notes", __name__, url_prefix="/api/day-notes")


@bp.get("")
def read_note():
    """某天的记事（`?date=YYYY-MM-DD`，不传就是今天）。

    那天没写过就返回**空字符串**，不是 404：对界面来说"还没写"和"写过又清空了"
    是同一件事（都该显示一个空框），让它去分辨 404 只会多一层没意义的判断。
    """
    try:
        target = todo_service.parse_date(request.args.get("date"))
    except ValueError as exc:
        return fail(str(exc))
    return ok(todo_service.get_note(target).to_dict())


@bp.put("")
def save_note():
    """整体覆盖某天的记事。

    用 PUT 而不是 PATCH：前端每次交上来的就是**整段文字**——记事本没有"改一个
    字段"这回事，一次输入就是一次全文替换。空串也是合法值（把那天的记事清空）。
    """
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return fail("请求体应该是一个 JSON 对象")

    try:
        target = todo_service.parse_date(data.get("date"))
        note = todo_service.save_note(target, data.get("text"))
    except ValueError as exc:
        return fail(str(exc))
    return ok(note.to_dict())
