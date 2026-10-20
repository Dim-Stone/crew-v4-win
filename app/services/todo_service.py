"""主页"今天"卡片：某一天的待办 + 那一天的记事本。

第一版只有"显示 + 新增"，理由是"不留半成品的字段"。用户后来要了完成态、删除、
按日期过滤、还有记事本，所以这里补全。**改动时留意 `target_date`**：所有待办查询
都按它在"某一天"的范围里取，不再有"全部待办"这个视角——这条一破，切日期就会串味。

为什么单开一个 service，而不是塞进 curriculum_service：
    待办与课程毫无关系（它不读课程，也不影响授课）。放一起只会让那个文件的
    关注点变杂。

日期在内部一律用 `datetime.date` 流转，不用字符串：
    字符串比较跨月、跨年时是**静默出错**的（`"2026-9-3" > "2026-10-07"`），
    而 `date` 对象不会。解析只在边界上做一次（`parse_date`）。
"""

from __future__ import annotations

from datetime import date, datetime

from ..extensions import db
from ..models import DayNote, TodoItem

#: 一条待办最多这么长。挡住"把整篇文章粘进来"——那不是待办，而且会把那一块撑坏。
MAX_TEXT = 300

#: 记事本的上限。比待办宽得多（它本来就是"想写什么就写什么"），但仍挡住"粘一整本书"。
MAX_NOTE = 20000

#: 日期在 URL / JSON 里的写法。**只有这一处**写它。
DATE_FORMAT = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------

def parse_date(value: object, *, default: date | None = None) -> date:
    """把界面给的日期解析成 `date`；坏值抛 `ValueError`（接口层转成 400）。

    为什么校验得这么死：日期是这个功能的**键**——解析错了会静默写到另一天去，
    而用户从界面上完全看不出来（他只会觉得"我写的东西丢了"）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default or date.today()
    if isinstance(value, datetime):        # datetime 是 date 的子类，先摘掉
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("日期要写成 YYYY-MM-DD")
    try:
        return datetime.strptime(value.strip(), DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(f"日期格式不对（要 YYYY-MM-DD）：{value}") from exc


def iso(value: date) -> str:
    """反过来：`date` → `YYYY-MM-DD`。和 `parse_date` 一起保证格式只有一处。"""
    return value.strftime(DATE_FORMAT)


# ---------------------------------------------------------------------------
# 待办
# ---------------------------------------------------------------------------

def list_items(target: date, limit: int = 200) -> list[TodoItem]:
    """**某一天的待办，按写下的先后排。**

    为什么不按"未完成在前"排（第一版是那样，改掉了）：勾一下那一行会当场跳到
    底部去，而用户正想接着勾下一条——他刚点的那条已经不在眼皮底下了。这一天
    的清单是条时间线，**位置稳定**比分组更要紧；完成与否用删除线表示就够了
    （见 `app.css` 的 `.todo-item.is-done`）。

    `limit` 是防呆上限——单用户本地用，一天堆到 200 条已经不是清单了。
    """
    return (TodoItem.query
            .filter(TodoItem.target_date == target)
            .order_by(TodoItem.id.asc())
            .limit(limit)
            .all())


def add_item(text: object, target: date) -> TodoItem:
    """在某一天新增一条。

    空白、超长、以及**根本不是文字**（接口是公开的，`{"text": 123}` 也会打进来）
    一律抛 `ValueError`，由接口层转成 400。校验写在这里，是为了让"什么算合法的一条"
    只有一个出处。

    为什么专门挡类型：`(text or "").strip()` 遇到数字或对象会抛 `AttributeError`，
    那会变成 500——一个"参数写错"被报成"服务器出错"，排查方向就被带偏了。
    """
    if text is None:
        raise ValueError("待办内容不能为空")
    if not isinstance(text, str):
        raise ValueError("待办内容必须是一段文字")

    cleaned = text.strip()
    if not cleaned:
        raise ValueError("待办内容不能为空")
    if len(cleaned) > MAX_TEXT:
        raise ValueError(f"一条待办最多 {MAX_TEXT} 个字")

    item = TodoItem(text=cleaned, target_date=target)
    db.session.add(item)
    db.session.commit()
    return item


def set_done(item_id: object, done: object) -> TodoItem:
    """勾上 / 取消。

    `done` **只接受真正的布尔**：接口那边可能传 `"false"`，而字符串在 Python 里
    是真值——`if done:` 会把"取消勾选"当成"勾上"，症状是"点一下没反应、再点还是勾着"。
    """
    item = _get(item_id)
    if not isinstance(done, bool):
        raise ValueError("done 只能是 true 或 false")
    item.done = done
    db.session.commit()
    return item


def delete_item(item_id: object) -> int:
    """删掉一条。返回被删的 id（接口原样回给前端，便于它做动画/提示）。"""
    item = _get(item_id)
    db.session.delete(item)
    db.session.commit()
    return item.id


def _get(item_id: object) -> TodoItem:
    """按 id 取一条；不存在抛 `LookupError`（接口层转成 404）。"""
    try:
        key = int(item_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("待办编号不是一个数字") from exc
    item = db.session.get(TodoItem, key)
    if item is None:
        raise LookupError("这条待办已经不在了（可能已经被删掉）")
    return item


# ---------------------------------------------------------------------------
# 记事本
# ---------------------------------------------------------------------------

def get_note(target: date) -> DayNote:
    """某天的记事本。

    那天还没写过就返回一份**空的临时对象**，不往库里建行——"看一眼"不该写库
    （否则浏览过去的每一天都会在库里留下一条空记录）。
    """
    note = db.session.get(DayNote, target)
    return note if note is not None else DayNote(date=target, text="")


def save_note(target: date, text: object) -> DayNote:
    """整体覆盖某天的记事本（前端每次传整段文字，不做增量）。"""
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValueError("记事内容必须是一段文字")
    if len(text) > MAX_NOTE:
        raise ValueError(f"记事最多 {MAX_NOTE} 个字")

    note = db.session.get(DayNote, target)
    if note is None:
        note = DayNote(date=target, text=text)
        db.session.add(note)
    else:
        note.text = text
    db.session.commit()
    return note
