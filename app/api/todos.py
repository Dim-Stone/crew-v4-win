"""主页"今天"卡片里的待办接口。

第一版只有"列出 + 新增"。现在补齐**按日期取、勾选、删除**三件事——
界面上那三行动作（打勾、删除线、叉掉）都要有对应的一句话。

**日期是这个接口的键**：不传 `date` 就是"今天"（`todo_service.parse_date` 兜底），
传了就必须是 `YYYY-MM-DD`——坏值一律 400，不猜。
"""

from __future__ import annotations

from flask import Blueprint, request

from ..services import todo_service
from ._common import fail, ok

bp = Blueprint("todos", __name__, url_prefix="/api/todos")


def _body() -> dict | None:
    """请求体，不是 JSON 对象就返回 None。

    为什么要挡：请求体是 JSON 数组/数字时 `data.get` 会抛 AttributeError → 500。
    那跟"参数写错却被报成服务器出错"是同一类问题，当场挡成 400。
    """
    data = request.get_json(silent=True)
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def _target(data: dict):
    """从请求里取日期。坏值抛 ValueError，由调用方转 400。"""
    return todo_service.parse_date(data.get("date"))


@bp.get("")
def list_todos():
    """某一天的待办。`?date=YYYY-MM-DD`，不传就是今天。"""
    try:
        target = todo_service.parse_date(request.args.get("date"))
    except ValueError as exc:
        return fail(str(exc))

    return ok({
        "date": todo_service.iso(target),
        "items": [item.to_dict() for item in todo_service.list_items(target)],
    })


@bp.post("")
def create_todo():
    data = _body()
    if data is None:
        return fail("请求体应该是一个 JSON 对象")

    try:
        item = todo_service.add_item(data.get("text"), _target(data))
    except ValueError as exc:
        return fail(str(exc))
    return ok(item.to_dict())


@bp.patch("/<int:item_id>")
def update_todo(item_id: int):
    """勾上 / 取消（`{"done": true}`）。

    用 PATCH 而不是 PUT：界面上改的是**一个字段**（那一行有没有被勾掉），
    其它字段原样不动。PUT 的语义是"整份替换"，会把没传的字段的含义说糊。
    """
    data = _body()
    if data is None:
        return fail("请求体应该是一个 JSON 对象")

    try:
        item = todo_service.set_done(item_id, data.get("done"))
    except ValueError as exc:
        return fail(str(exc))
    except LookupError as exc:
        return fail(str(exc), status=404)
    return ok(item.to_dict())


@bp.delete("/<int:item_id>")
def delete_todo(item_id: int):
    try:
        deleted = todo_service.delete_item(item_id)
    except ValueError as exc:
        return fail(str(exc))
    except LookupError as exc:
        return fail(str(exc), status=404)
    return ok({"deleted": deleted})
