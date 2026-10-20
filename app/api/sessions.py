"""学习会话与教学回合接口。"""

from __future__ import annotations

from flask import Blueprint, request

from ..extensions import db
from ..models import LessonPage, Message
from ..services import curriculum_service, page_service, quiz_service, teaching_service
from ._common import active_turn_for_session, fail, ok, start_turn

bp = Blueprint("sessions", __name__, url_prefix="/api/sessions")


@bp.post("")
def create_session():
    data = request.get_json(silent=True) or {}
    course_id = data.get("course_id")
    try:
        session = curriculum_service.create_session(course_id)
    except ValueError as exc:
        return fail(str(exc), 404)
    return ok({"id": session.id})


@bp.get("/<int:session_id>")
def get_session(session_id: int):
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)
    return ok(teaching_service.session_to_dict(session))


@bp.post("/<int:session_id>/messages")
def post_message(session_id: int):
    """发一条消息给讲师。立刻返回 turn_id，内容走 SSE。"""
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return fail("消息不能为空")

    # 课堂里的"这一步讲完了，我说一句"与对话里的随口提问**不一样**：
    # 前者意味着"接着讲下一步"，后者不该翻步。
    # 用显式的 intent 区分，而不是靠猜措辞（猜错过很多次）。
    intent = str(data.get("intent") or "").strip()
    advance_step = intent == "feedback"

    # 并发闸：同一会话只允许一轮教学在跑。
    # **必须在写入用户消息之前判断**——否则被拒的那句话会留在历史里，
    # 而它并没有被讲师回答，下一次加载看起来就像"我发了他没回"。
    busy = active_turn_for_session(session_id, ("teach",))
    if busy is not None:
        return fail("这个会话还有一轮正在讲，等它结束再发", 409)

    # 关联三元组要在**写入时**就带上，不要指望事后从文本标签里推断：
    #   · `point_id` 决定这条消息属于哪个知识点（历史分组、图解取材都靠它）；
    #   · `course_id` 让"跨课程检索/统计"成为可能；
    #   · `turn_id` 在 start_turn 之后才存在，所以下面补写一次。
    message = Message(
        session_id=session_id,
        role="user",
        content=content,
        tags="[]",
        course_id=session.course_id,
        point_id=session.current_point_id,
    )
    db.session.add(message)
    db.session.commit()

    turn = start_turn(
        "teach",
        teaching_service.make_teach_worker(session_id, content, advance_step=advance_step),
        session_id=session_id,
        payload={"content": content},
    )

    # 补上 turn_id：这样"这轮教学产生了什么"可以顺着 Message → Turn 追下去
    message.turn_id = turn.id
    db.session.commit()

    return ok({"turn_id": turn.id})


@bp.post("/<int:session_id>/goto-point")
def goto_point(session_id: int):
    """切到指定知识点（课程页左栏点击章节时调用）。"""
    data = request.get_json(silent=True) or {}
    point_id = data.get("point_id")
    if point_id is None:
        return fail("缺少 point_id")
    try:
        result = teaching_service.goto_point(session_id, int(point_id))
    except (ValueError, TypeError) as exc:
        return fail(str(exc), 404)
    return ok(result)


@bp.post("/<int:session_id>/next-point")
def next_point(session_id: int):
    """把当前知识点标记为已讲，推进到下一个。

    **前端入口已撤下**（课程页那个「下一个知识点」按钮删了，切换知识点只走左栏），
    所以这个路由现在没有界面调用者——它保留给外部脚本与将来的入口，不是漏删。

    另：授课回合里"讲到下一节"仍会自己翻页（`teaching_service.advance_point` 被
    授课流程直接调用），那条路径与此路由无关。
    """
    try:
        result = teaching_service.advance_point(session_id)
    except ValueError as exc:
        return fail(str(exc), 404)
    return ok(result)


@bp.post("/<int:session_id>/diagram")
def make_diagram(session_id: int):
    """可选的图解动作：点了按钮才跑。"""
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    data = request.get_json(silent=True) or {}
    style_hint = (data.get("style_hint") or "").strip()

    # 图解与教学回合互斥：两者都要写 Message、都要往同一个讲义文件追加。
    # 图解自己也占一个 thread，两个图解同时跑还会争同一份素材。
    busy = active_turn_for_session(session_id, ("teach", "diagram"))
    if busy is not None:
        return fail("这个会话还有任务在跑，等它结束再画图", 409)

    turn = start_turn(
        "diagram",
        teaching_service.make_diagram_worker(session_id, style_hint),
        session_id=session_id,
        payload={"style_hint": style_hint},
    )
    return ok({"turn_id": turn.id})


@bp.get("/<int:session_id>/timeline")
def timeline(session_id: int):
    """时间轴 / 标签视图的数据（对应 canvas 里"历史记录 + 标签 + 时间轴"的诉求）。"""
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    items = []
    for msg in session.messages:
        if msg.role != "agent":
            continue
        items.append({
            "id": msg.id,
            "agent": msg.agent,
            "tags": msg.to_dict()["tags"],
            "preview": (msg.content or "")[:120],
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        })
    return ok({"items": items})


# ---------------- 卡片课堂（P0） ----------------

@bp.get("/<int:session_id>/deck")
def get_deck(session_id: int):
    """这一节的卡片全貌：页列表 + 环节状态 + 课程进度。

    **刷新恢复靠它**：页面重新加载后，前端拿到的"当前第几页、这一节在哪个环节"
    必须来自数据库，而不是内存里的状态——用户完全可能关掉窗口再打开。
    """
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    point_id = request.args.get("point_id", type=int)
    return ok(page_service.deck(session, point_id))


@bp.post("/<int:session_id>/pages")
def next_page(session_id: int):
    """生成"下一页"讲授：本质上就是让讲师继续讲。

    ## 为什么不是"前端自己翻页"

    卡片有两种翻页：**看已经有的页**（前端本地导航）与**生成新的一页**（这里）。
    后者要跑一次模型，必须走 `start_turn`，才有超时/取消/断线恢复。

    ## 它怎么知道"该讲下一步了"

    讲义进度本来就由两件事推进：回合后的复盘（`review_turn` 更新
    `current_step`）与"同一步讲太多轮就强制前进"的兜底。而授课 worker
    在开讲前会 `wait_for_side(session_id)` 等上一轮复盘做完——
    所以这里只要把"继续"传下去，通常就落在**下一步**上。

    如果复盘判定还没讲透（真的还在同一步），那就再讲一遍同一步：
    这正是想要的行为（宁可多讲一遍，也不要跳过一个没讲清的步骤）。
    学生想强制往下走时，用"跳过本页"或左栏切知识点。

    `instruction` 可以覆盖默认的"继续讲下一步"（比如学生说"再举个例子"）。
    """
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    busy = active_turn_for_session(session_id, ("teach",))
    if busy is not None:
        return fail("这个会话还有一轮正在讲，等它结束再翻页", 409)

    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip() or "继续讲下一步"
    page_id = data.get("page_id")

    turn = start_turn(
        "teach",
        # 「继续讲」= 明确要求往下走：开讲前把教案推进到下一步
        teaching_service.make_teach_worker(session_id, instruction, advance_step=True),
        session_id=session_id,
        payload={"intent": "next_page", "page_id": page_id, "instruction": instruction},
    )
    return ok({"turn_id": turn.id})


@bp.post("/<int:session_id>/pages/<int:page_id>/skip")
def skip_page(session_id: int, page_id: int):
    """跳过本页。只改状态，不删内容（学生可能回头还要看）。"""
    page = db.session.get(LessonPage, page_id)
    if page is None or page.session_id != session_id:
        return fail("这一页不存在", 404)
    page_service.skip_page(page)
    return ok(page.to_dict())


@bp.post("/<int:session_id>/chapter-quiz")
def start_chapter_quiz(session_id: int):
    """章末测试：覆盖整章的重难点，按知识点归因。

    与小测共用同一套判分与信号；区别在 scope="chapter" 与题数（默认 10）。
    章节不给就取当前会话所在章。
    """
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    busy = active_turn_for_session(session_id, ("quiz",))
    if busy is not None:
        return fail("正在出题，稍等一下", 409)

    data = request.get_json(silent=True) or {}
    chapter_id = data.get("chapter_id") or session.current_chapter_id
    if not chapter_id:
        return fail("还没有进入任何章节", 400)

    count = quiz_service.clamp_chapter_count(data.get("count") or 10)
    types = quiz_service.normalize_types(data.get("types"))

    turn = start_turn(
        "quiz",
        quiz_service.make_chapter_quiz_worker(
            session_id, chapter_id=int(chapter_id), count=count, types=types
        ),
        session_id=session_id,
        payload={"scope": "chapter", "chapter_id": chapter_id, "count": count},
    )
    return ok({"turn_id": turn.id, "chapter_id": int(chapter_id),
               "count": count, "types": list(types)})


@bp.get("/<int:session_id>/chapter-quiz")
def latest_chapter_quiz(session_id: int):
    """这一章最近的一份章末测试（没有就 null）。"""
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    chapter_id = request.args.get("chapter_id", type=int) or session.current_chapter_id
    if not chapter_id:
        return ok({"quiz": None})
    quiz = quiz_service.latest_chapter_quiz(session_id, int(chapter_id))
    return ok({"quiz": quiz.to_dict() if quiz else None})


@bp.get("/<int:session_id>/quiz")
def latest_quiz(session_id: int):
    """这一节最近的一份小测（没有就返回 null）。

    用途：课堂页刷新 / 从对话页跳回来时，要能回答"我出过题没有、判完了没有"。
    出题动作在 `POST /api/sessions/<id>/quiz`，取题面在 `GET /api/quizzes/<id>`。
    """
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    quiz = quiz_service.latest_quiz(session_id, session.current_point_id)
    if quiz is None:
        return ok({"quiz": None})
    return ok({"quiz": quiz.to_dict()})


@bp.post("/<int:session_id>/quiz")
def start_quiz(session_id: int):
    """出一份小测。一个知识点一次，默认 5 题、三种题型。

    题数与题型由前端传（`count` / `types`）：
      · 题数夹在 3–10（少于 3 题给不出可信的掌握度，多于 10 题学生烦）；
      · 题型只认 选择 / 多选 / 填空 / 判断，别的静默丢掉。
    出题要跑模型，所以走 `start_turn()`；生成本身由 `quiz` 事件通知前端
    （带 quiz_id），前端再去 `GET /api/quizzes/<id>` 取题面。
    """
    session = teaching_service.get_session(session_id)
    if session is None:
        return fail("学习会话不存在", 404)

    busy = active_turn_for_session(session_id, ("quiz",))
    if busy is not None:
        return fail("正在出题，稍等一下", 409)

    data = request.get_json(silent=True) or {}
    count = quiz_service.clamp_count(data.get("count"))
    types = quiz_service.normalize_types(data.get("types"))
    page_id = data.get("page_id")

    turn = start_turn(
        "quiz",
        quiz_service.make_quiz_worker(session_id, count=count, types=types,
                                      page_id=page_id),
        session_id=session_id,
        payload={"count": count, "types": list(types), "page_id": page_id},
    )
    return ok({"turn_id": turn.id, "count": count, "types": list(types)})
