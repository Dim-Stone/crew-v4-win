"""把讲授落成"页"，并提供这一节卡片的查询与翻页状态。

分工说清楚：
    · `page_builder` 只管**怎么切**（纯函数，可单测）；
    · 本模块管**切完之后怎么存、怎么取**（页序、状态、环节）；
    · 页面怎么摆是前端的事，这里只给"有哪些页、当前停在第几页"。

## 为什么页序要显式分配，而不是用自增 id 排序

自增 id 反映的是"插入顺序"，不是"这一节里的第几页"。并发或重讲时
（同一页被"再讲一遍"）插入顺序会和阅读顺序错位。所以每次落页都在
**同一事务里**取当前最大值 +1，保证 `(session, point)` 内 index 单调且无洞。
"""

from __future__ import annotations

import json

from ..extensions import db
from ..models import Chapter, KnowledgePoint, LessonPage, LessonPlan, StudySession
from . import page_builder, quiz_service

#: 一页的状态取值。`running` 留给"先占位再流式填内容"的场景（P1 出题会用到）。
PAGE_READY = "ready"
PAGE_SKIPPED = "skipped"
PAGE_FAILED = "failed"


def _point_of(session: StudySession) -> KnowledgePoint | None:
    if session.current_point_id is None:
        return None
    return db.session.get(KnowledgePoint, session.current_point_id)


def pages_for(session_id: int, point_id: int | None) -> list[LessonPage]:
    """这一节已有的页，按阅读顺序。"""
    if point_id is None:
        return []
    return (
        LessonPage.query
        .filter_by(session_id=session_id, point_id=point_id)
        .order_by(LessonPage.index, LessonPage.id)
        .all()
    )


def record_pages(
    *,
    session: StudySession,
    point: KnowledgePoint | None,
    plan: LessonPlan | None,
    content: str,
    materials: list[str] | None = None,
    turn_id: int | None = None,
) -> list[LessonPage]:
    """把一轮讲授正文切成页并落库。返回新页（按顺序）。

    **一条消息可能产生多页**——这正是"超长自动分页"的落点。
    内容为空、或这个会话没有当前知识点时，不建页（返回空列表）：
    宁可让学生看到的还是对话气泡，也不要造一页空白卡片。
    """
    if point is None:
        return []

    chunks = page_builder.paginate(content or "")
    if not chunks:
        return []

    existing = pages_for(session.id, point.id)
    next_index = (existing[-1].index + 1) if existing else 0

    created: list[LessonPage] = []
    for offset, chunk in enumerate(chunks):
        page = LessonPage(
            session_id=session.id,
            point_id=point.id,
            plan_id=plan.id if plan is not None else None,
            step_index=plan.current_step if plan is not None else None,
            index=next_index + offset,
            kind="lecture",
            title=chunk["title"],
            content=chunk["content"],
            status=PAGE_READY,
            turn_id=turn_id,
            materials=json.dumps(materials or [], ensure_ascii=False),
        )
        db.session.add(page)
        created.append(page)

    # 先 flush 拿到 id：调用方可能马上要用第一页的 id 回填 Message.page_id
    db.session.flush()

    if plan is not None and not plan.stage:
        plan.stage = "lecturing"
    return created


def skip_page(page: LessonPage) -> LessonPage:
    """标记"跳过本页"。

    只改状态，不删内容：学生可能只是想先往下走，回头还要看。
    跳过也不产生任何掌握度证据（与"跳过测试"同一个原则）。
    """
    page.status = PAGE_SKIPPED
    db.session.commit()
    return page


def deck(session: StudySession, point_id: int | None = None) -> dict:
    """这一节的卡片全貌——**刷新恢复的唯一依据**。

    `point_id` 不给就用会话当前知识点。返回里同时带"课程进度"与"教案进度"，
    因为页脚要显示"第 2/5 页"，左栏要显示"知识点 3/12"，两者来源不同。
    """
    from . import curriculum_service  # 函数内导入：避开 services 包启动期的循环引用

    point = _point_of(session) if point_id is None else db.session.get(KnowledgePoint, point_id)
    course = None
    if session.course_id is not None:
        from ..models import Course

        course = db.session.get(Course, session.course_id)

    pages = pages_for(session.id, point.id if point else None)
    plan = None
    if point is not None:
        plan = LessonPlan.query.filter_by(point_id=point.id).first()

    chapter = db.session.get(Chapter, point.chapter_id) if point is not None else None
    chapter_index = point_index = total_points = 0
    if course is not None and point is not None:
        chapter_index, point_index = curriculum_service.point_position(course, point.id)
        total_points = sum(len(item.points) for item in course.chapters)

    steps = plan.steps_list() if plan is not None else []
    # 最近一次小测：课堂页刷新后要靠它回答"出过题没有、判完了没有"。
    quiz = quiz_service.latest_quiz(session.id, point.id if point else None)
    return {
        "session_id": session.id,
        "course_id": session.course_id,
        "point": {
            "id": point.id if point else None,
            "title": point.title if point else "",
            "chapter_title": chapter.title if chapter else "",
            "chapter_index": chapter_index,
            "point_index": point_index,
            "total_points": total_points,
        },
        "plan": {
            "id": plan.id if plan else None,
            "stage": (plan.stage or "lecturing") if plan else "lecturing",
            "current_step": plan.current_step if plan else 0,
            "total_steps": len(steps),
            "quiz_skipped": bool(plan.quiz_skipped) if plan else False,
            "finished": plan.is_finished() if plan else False,
        },
        "pages": [page.to_dict() for page in pages],
        "count": len(pages),
        # 刷新后应该停在最后一页（内容最新）；前端自己记住"用户翻到过哪"
        "current_index": pages[-1].index if pages else -1,
        "quiz": (
            {
                "id": quiz.id,
                "status": quiz.status,
                "score": quiz.score,
                "count": len(quiz.items()),
                "weak_steps": [int(one) for one in _json_list(quiz.weak_steps)],
            }
            if quiz is not None
            else None
        ),
    }


def _json_list(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
