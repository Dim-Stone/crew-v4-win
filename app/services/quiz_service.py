"""小测：出题、判分、以及"这场小测留下了什么证据"。

## 三件事分开做，各有各的理由

1. **出题交给 `quizmaster`**（模型）——它知道什么叫重点、什么容易混。
2. **判分用规则**（本模块）——选择题比集合、判断题比键、填空题归一化后比字符串。
   分数后面要进掌握度，而"同一份答案两次判分不同"比"没有分数"更糟。
3. **证据写成一条结构化信号**（`quiz_result`）——`observer` 直接吃，
   不需要再用模型解释一遍测试结果（省一次调用，也不会两个模型互相矛盾）。

## 跳过 = 没有证据

`skip()` 把状态记成 `skipped`、`score=None`，信号里 `skipped: true`。
掌握度那边必须据此**不加分**——把"跳过"算成"通过"是最容易犯也最伤人的错。
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from fractions import Fraction

from ..extensions import db
from ..models import KnowledgePoint, LessonPage, LessonPlan, Quiz, QuizAnswer, StudySession
from . import profile_service
from .runtime import current_turn_id

#: 默认题数。少于 3 题给不出像样的掌握度，多于 10 题学生会烦。
DEFAULT_COUNT = 5
MIN_COUNT = 3
MAX_COUNT = 10

#: 支持的题型。
#:
#: **这里再写一遍而不是 `from ..agents import quizmaster` 取**：后者会在模块导入时
#: 把 crewai 整条链拖进来，而"只起应用不加载智能体"是打包探针专门守着的一条线
#: （`tests/test_package_size.py`）。出题本身在函数里延迟导入——那条路径本来就要跑模型。
#: 两处定义的一致性由 `tests/test_quiz.py` 的用例守着。
ALL_TYPES = ("single_choice", "multi_choice", "fill", "judge")


def normalize_types(raw) -> tuple[str, ...]:
    """把前端传来的题型收拾干净：只留认识的、去重、保序。"""
    if not raw:
        return ALL_TYPES
    if isinstance(raw, str):
        raw = [raw]
    picked = []
    for item in raw:
        name = str(item).strip()
        if name in ALL_TYPES and name not in picked:
            picked.append(name)
    return tuple(picked) or ALL_TYPES


def clamp_chapter_count(raw) -> int:
    """章末测试的题数：默认 10，上限 15。

    为什么要单独一条：章末要覆盖整章，题数天然比每节小测多；
    但再多学生也做不完（而且出题成本上升），所以给一个上限。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(5, min(15, value))


def clamp_count(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    return max(MIN_COUNT, min(MAX_COUNT, value))


# ---------------------------------------------------------------- 出题输入

def _steps_text(plan: LessonPlan | None) -> str:
    """这一节的步骤，做成一段给模型看的话。

    每一步只留标题、意图与前三个要点：出题靠这几句就够了，
    把 key_points 全塞进去只会让输出变长、变慢（出题慢的主要成本是**输出**）。
    """
    steps = plan.steps_list() if plan is not None else []
    if not steps:
        return ""
    lines = []
    for index, step in enumerate(steps[:10]):
        points = "、".join((step.get("key_points") or [])[:3])
        line = f"{index}. {step.get('title', '')}"
        if step.get("intent"):
            line += f"（要达成：{step['intent']}）"
        if points:
            line += f"；要点：{points}"
        lines.append(line)
    return "\n".join(lines)


def _focus_text(plan: LessonPlan | None) -> str:
    """这一节的**重点与难点**：直接取教案里的 pitfalls / checks。

    用户明确要求：出题按"知识本身考察的能力与内容"，不要迁就某个学生的水平。
    所以这里**不喂画像**，只喂"这一节最容易错什么、要检验什么"——
    这两项本来就是备课阶段为学生容易踩的坑写下的，正是难点所在。
    """
    if plan is None:
        return ""
    from ..models import json_list

    blocks: list[str] = []
    if (plan.goal or "").strip():
        blocks.append(f"学完应当能做到：{plan.goal.strip()}")
    pitfalls = [str(one).strip() for one in json_list(plan.pitfalls) if str(one).strip()]
    if pitfalls:
        blocks.append("常见错误：\n" + "\n".join(f"- {one}" for one in pitfalls[:6]))
    checks = [str(one).strip() for one in json_list(plan.checks) if str(one).strip()]
    if checks:
        blocks.append("备课阶段想检验的点：\n" + "\n".join(f"- {one}" for one in checks[:6]))
    return "\n\n".join(blocks)


def _materials_text(point: KnowledgePoint | None) -> str:
    """这个知识点用到的资料位置（只给位置，不塞原文）。

    为什么不塞原文：出题靠的是"这一节讲了什么"，原文里与这一步无关的几页
    只会让模型抓到别处去出题，而且输出会跟着变长、变慢。
    资料的作用是**约束**"不许编资料外的事实"，给位置就够了。
    """
    if point is None:
        return ""
    try:
        sources = json.loads(point.sources or "[]")
    except (TypeError, ValueError):
        return ""
    if not isinstance(sources, list):
        return ""
    lines = []
    for item in sources[:6]:
        if isinstance(item, dict) and str(item.get("file") or "").strip():
            name = str(item["file"]).strip()
            locator = str(item.get("locator") or "").strip()
            lines.append(f"{name} {locator}".strip())
    return "\n".join(lines)


# ---------------------------------------------------------------- 出题落库

def create_quiz(
    *,
    session: StudySession,
    count: int,
    types: tuple[str, ...],
    page: LessonPage | None = None,
) -> Quiz:
    """出一次题并落库。模型/解析失败会抛异常，由调用方（worker）如实报出来。"""
    # 延迟导入：这一行会把 crewai 拖进来，而"起应用"的路径不该付这个代价
    from ..agents import quizmaster

    point = None
    if session.current_point_id is not None:
        point = db.session.get(KnowledgePoint, session.current_point_id)

    plan = LessonPlan.query.filter_by(point_id=point.id).first() if point else None

    from ..models import Course

    course = db.session.get(Course, session.course_id) if session.course_id else None
    chapter_title = ""
    if point is not None and point.chapter is not None:
        chapter_title = point.chapter.title

    data = quizmaster.draft_quiz(
        course_title=course.title if course else "",
        chapter_title=chapter_title,
        point_title=point.title if point else "",
        point_summary=point.summary if point else "",
        steps_text=_steps_text(plan),
        # 只喂"这一节的重点与难点"，**不喂画像**：
        # 用户要求按知识本身考察的能力出题，而不是按这个学生当下的水平。
        focus_text=_focus_text(plan),
        materials_text=_materials_text(point),
        count=count,
        types=types,
        strict_materials=bool(course and course.material_mode == "strict"),
    )

    quiz = Quiz(
        session_id=session.id,
        point_id=point.id if point else None,
        page_id=page.id if page is not None else None,
        turn_id=current_turn_id(),
        payload=json.dumps({"items": data["items"]}, ensure_ascii=False),
        wanted_count=count,
        types=json.dumps(list(types), ensure_ascii=False),
        status="open",
    )
    db.session.add(quiz)
    db.session.commit()
    return quiz


def create_chapter_quiz(
    *,
    session: StudySession,
    chapter,
    count: int,
    types: tuple[str, ...],
) -> Quiz:
    """章末测试：覆盖整章的重难点，按知识点归因。

    与小测的区别（`scope="chapter"`）：
        · 输入是**整章所有知识点的教案**（目标 / 步骤要点 / 常见错误），不只是当前点；
        · 题目按 `point_id` 归因，判分后聚合出"哪个知识点薄弱"；
        · 题数更大（默认 10），并要求每个知识点至少一题。
    """
    from ..agents import quizmaster
    from ..models import Chapter, LessonPlan

    chapter = db.session.get(Chapter, chapter.id) if chapter is not None else None
    if chapter is None:
        raise ValueError("这一章不存在")

    points_payload: list[dict] = []
    for point in chapter.points:
        plan = LessonPlan.query.filter_by(point_id=point.id).first()
        if plan is None:
            continue
        points_payload.append({
            "id": point.id,
            "title": point.title,
            "goal": (plan.goal or "").strip(),
            "steps": _steps_text(plan),
            "pitfalls": "；".join(
                [str(one) for one in json.loads(plan.pitfalls or "[]")][:4]
            ) if plan.pitfalls else "",
        })

    if not points_payload:
        raise ValueError("这一章还没有可考的内容（先上几节课再来）")

    from ..models import Course

    course = db.session.get(Course, session.course_id) if session.course_id else None

    data = quizmaster.draft_chapter_quiz(
        course_title=course.title if course else "",
        chapter_title=chapter.title,
        chapter_summary=chapter.summary or "",
        points=points_payload,
        count=count,
        types=types,
        strict_materials=bool(course and course.material_mode == "strict"),
    )

    quiz = Quiz(
        session_id=session.id,
        point_id=session.current_point_id,
        chapter_id=chapter.id,
        scope="chapter",
        turn_id=current_turn_id(),
        payload=json.dumps({"items": data["items"]}, ensure_ascii=False),
        wanted_count=count,
        types=json.dumps(list(types), ensure_ascii=False),
        status="open",
    )
    db.session.add(quiz)
    db.session.commit()
    return quiz


def make_chapter_quiz_worker(session_id: int, *, chapter_id: int, count: int,
                             types: tuple[str, ...]):
    """线程池里跑的章末出题任务。"""

    def worker(publish) -> str:
        from ..models import Chapter

        session = db.session.get(StudySession, session_id)
        if session is None:
            raise ValueError("学习会话不存在")

        publish({"type": "status", "status": "整理整章内容"})
        chapter = db.session.get(Chapter, chapter_id)
        if chapter is None:
            raise ValueError("这一章不存在")

        publish({"type": "status", "status": "出题中"})
        quiz = create_chapter_quiz(session=session, chapter=chapter,
                                   count=count, types=types)
        publish({
            "type": "quiz",
            "quiz_id": quiz.id,
            "scope": "chapter",
            "count": len(quiz.items()),
        })
        return f"章末测试出了 {len(quiz.items())} 道题"

    return worker


def latest_chapter_quiz(session_id: int, chapter_id: int) -> Quiz | None:
    return (
        Quiz.query
        .filter_by(session_id=session_id, chapter_id=chapter_id, scope="chapter")
        .order_by(Quiz.id.desc())
        .first()
    )


def latest_quiz(session_id: int, point_id: int | None) -> Quiz | None:
    """这一节最近的一份小测（可能还没作答）。

    课堂页刷新时要靠它回答"我刚出过题没有、判完了没有"——没有它，
    刷新之后小测就像没发生过一样。
    """
    query = Quiz.query.filter_by(session_id=session_id)
    if point_id is not None:
        query = query.filter(Quiz.point_id == point_id)
    return query.order_by(Quiz.id.desc()).first()


def make_quiz_worker(session_id: int, *, count: int, types: tuple[str, ...],
                     page_id: int | None = None):
    """线程池里跑的出题任务。"""

    def worker(publish) -> str:
        session = db.session.get(StudySession, session_id)
        if session is None:
            raise ValueError("学习会话不存在")

        page = db.session.get(LessonPage, page_id) if page_id else None
        publish({"type": "status", "status": "出题中"})
        quiz = create_quiz(session=session, count=count, types=types, page=page)
        publish({
            "type": "quiz",
            "quiz_id": quiz.id,
            "count": len(quiz.items()),
            "point_id": quiz.point_id,
        })
        return f"出了 {len(quiz.items())} 道题"

    return worker


# ---------------------------------------------------------------- 判分

def _norm(text: str) -> str:
    """把答案归一化：全角转半角、去空白、大小写不敏感。

    为什么必须做：学生手打 "１／２"、"1/2 "、"1 / 2" 都是同一个答案，
    而模型给的参考答案也可能带全角符号。不归一化就会把对的判错——
    这种错比"没测"更伤害信任。
    """
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("，", ",").replace("。", ".").replace("＝", "=")
    value = "".join(value.split())
    return value.strip().lower()


def _as_number(text: str) -> Fraction | None:
    """把 "1/2"、"0.5"、" -1/3 " 都读成有理数，用于等价比较。"""
    value = _norm(text)
    if not value:
        return None
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


def _keys(response) -> list[str]:
    """把作答读成选项键的列表（兼容 ["A","C"] 和 "A,C"）。"""
    if isinstance(response, str):
        parts = response.replace("，", ",").split(",")
    elif isinstance(response, (list, tuple, set)):
        parts = list(response)
    else:
        return []
    return [str(part).strip().upper()[:1] for part in parts if str(part).strip()]


def grade_item(item: dict, response) -> dict:
    """判一道题。返回 `{correct, score, error_kind}`。

    `error_kind` 不是给界面看的装饰，它是**下一次怎么讲**的依据：
        · `blank` 空着 → 可能没时间/不会 → 下次先补这一步
        · `concept_confusion` 答了但错 → 概念混了 → 换个角度重讲
        · `careless` 接近但不等（数值题差一点） → 提醒检查
    """
    kind = item.get("type")
    answer = item.get("answer") or []

    if kind in ("single_choice", "multi_choice", "judge"):
        given = sorted(set(_keys(response)))
        want = sorted({str(one).upper()[:1] for one in answer})
        if not given:
            return {"correct": False, "score": 0.0, "error_kind": "blank"}
        correct = given == want
        return {
            "correct": correct,
            "score": 1.0 if correct else 0.0,
            "error_kind": "" if correct else "concept_confusion",
        }

    # 填空题
    text = _norm(response)
    if not text:
        return {"correct": False, "score": 0.0, "error_kind": "blank"}

    accepted = [_norm(one) for one in answer]
    if text in accepted:
        return {"correct": True, "score": 1.0, "error_kind": ""}

    # 数值等价："0.5" 与 "1/2" 应当算对
    given_number = _as_number(text)
    if given_number is not None:
        for one in accepted:
            number = _as_number(one)
            if number is not None and number == given_number:
                return {"correct": True, "score": 1.0, "error_kind": ""}
        # 数值接近但不相等：判错，但错因是"算错了"而不是"不懂"
        for one in accepted:
            number = _as_number(one)
            if number is not None and abs(number - given_number) <= abs(number) * 0.05:
                return {"correct": False, "score": 0.0, "error_kind": "careless"}

    return {"correct": False, "score": 0.0, "error_kind": "concept_confusion"}


def grade_quiz(quiz: Quiz, answers: dict) -> dict:
    """判一整份小测，落库并返回给前端的完整结果。"""
    items = quiz.items()
    # 重判时先清掉旧作答：一次小测只有一份有效作答，留两份会让统计翻倍
    QuizAnswer.query.filter_by(quiz_id=quiz.id).delete(synchronize_session=False)

    graded: list[dict] = []
    weak_steps: list[int] = []
    for item in items:
        item_id = str(item.get("id") or "")
        result = grade_item(item, (answers or {}).get(item_id))
        question = {
            "id": item_id,
            "type": item.get("type"),
            # 章末测试按知识点归因：逐题结果里**必须**带着它，
            # 否则聚合是空的（这个字段漏过一次，被 test_chapter_quiz 抓到）
            "point_id": item.get("point_id"),
            "step": item.get("step"),
            "stem": item.get("stem"),
            "options": item.get("options") or [],
            "answer": item.get("answer") or [],
            "explain": item.get("explain") or "",
            "response": (answers or {}).get(item_id),
            "source": item.get("source"),
            **result,
        }
        graded.append(question)

        db.session.add(QuizAnswer(
            quiz_id=quiz.id,
            item_id=item_id,
            item_type=str(item.get("type") or ""),
            step=item.get("step"),
            response=json.dumps(question["response"], ensure_ascii=False),
            correct=result["correct"],
            score=result["score"],
            error_kind=result["error_kind"],
        ))

        step = item.get("step")
        if not result["correct"] and isinstance(step, int) and step not in weak_steps:
            weak_steps.append(step)

    total = len(graded) or 1
    score = sum(one["score"] for one in graded) / total

    quiz.status = "graded"
    quiz.score = round(score, 4)
    quiz.weak_steps = json.dumps(sorted(weak_steps), ensure_ascii=False)

    # 章末测试：按知识点聚合。这是它比每节小测更有价值的地方——
    # "这一章哪个知识点没立起来"只能从这个聚合里看出来。
    aggregate: dict[str, dict] = {}
    if (quiz.scope or "point") == "chapter":
        from ..models import KnowledgePoint

        titles = {one.id: one.title for one in KnowledgePoint.query.all()}
        for question in graded:
            point_id = question.get("point_id")
            if not isinstance(point_id, int):
                continue
            bucket = aggregate.setdefault(
                str(point_id), {"title": titles.get(point_id, ""), "right": 0, "total": 0}
            )
            bucket["total"] += 1
            if question["correct"]:
                bucket["right"] += 1
        for bucket in aggregate.values():
            bucket["score"] = round(bucket["right"] / bucket["total"], 4) if bucket["total"] else 0.0
        quiz.aggregate = json.dumps(aggregate, ensure_ascii=False)

    quiz.graded_at = datetime.now(timezone.utc)
    db.session.commit()

    signal = _signal(quiz, graded, skipped=False)
    profile_service.add_event(quiz.turn_id, signal, note="小测结果")

    return {
        "quiz": quiz.to_dict(include_answers=True),
        "items": graded,
        "score": quiz.score,
        "weak_steps": sorted(weak_steps),
        "aggregate": aggregate,
        "signal": signal,
    }


def skip_quiz(quiz: Quiz, *, reason: str = "学生跳过") -> dict:
    """跳过测试：**没有分数、没有掌握度证据**，只留一条"未测"。"""
    quiz.status = "skipped"
    quiz.score = None
    quiz.graded_at = datetime.now(timezone.utc)
    db.session.commit()

    signal = _signal(quiz, [], skipped=True)
    profile_service.add_event(quiz.turn_id, signal, note=f"小测跳过（{reason}）")
    return {"quiz": quiz.to_dict(), "score": None, "skipped": True, "signal": signal}


def _signal(quiz: Quiz, graded: list[dict], *, skipped: bool) -> dict:
    """给 `observer` 吃的结构化信号。

    `confidence` 是给掌握度用的权重依据：3 题以内只能算"低置信"，
    4 题以上才算中等——一次两道题就宣布"掌握了"是不可接受的。
    """
    weak_steps = sorted({one["step"] for one in graded
                         if not one["correct"] and isinstance(one["step"], int)})
    return {
        "kind": "quiz_result",
        "quiz_id": quiz.id,
        "scope": quiz.scope or "point",
        "chapter_id": quiz.chapter_id,
        "point_id": quiz.point_id,
        "page_id": quiz.page_id,
        "aggregate": quiz.aggregate_dict(),
        "score": quiz.score,
        "items": [
            {
                "item_id": one["id"],
                "step": one["step"],
                "type": one["type"],
                "correct": one["correct"],
                "error": one["error_kind"],
            }
            for one in graded
        ],
        "weak_steps": weak_steps,
        "confidence": "medium" if len(graded) >= 4 else "low",
        "skipped": skipped,
    }
