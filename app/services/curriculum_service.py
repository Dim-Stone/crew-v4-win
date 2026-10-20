"""课程与大纲服务。

一次大纲生成 = 一个 Turn（异步）：
    规划师智能体产出结构化大纲 → 落库 Course/Chapter/KnowledgePoint。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..extensions import db
from ..materials import MODE_NONE, MODES, uses_materials
from ..models import Chapter, Course, KnowledgePoint, ProfileEvent, StudySession, Turn


# ---------------- 读写 ----------------

def create_course(
    direction: str,
    focus: str = "",
    title: str = "",
    *,
    material_mode: str = "",
    material_path: str = "",
) -> Course:
    """建一门课。

    `material_mode` 为空表示**不使用资料**（沿用原有的自由编排）；
    取 `strict` / `reference` 时走资料链路。不认识的值一律当成"不用资料"——
    宁可退回原有流程，也不要凭一个拼错的值就去读用户的磁盘。
    """
    mode = (material_mode or "").strip().lower()
    if mode not in MODES:
        mode = MODE_NONE

    course = Course(
        title=title.strip() or direction.strip()[:80],
        direction=direction.strip(),
        focus=focus.strip(),
        material_mode=mode,
        material_path=(material_path or "").strip() if mode else "",
        status="draft",
    )
    db.session.add(course)
    db.session.commit()
    return course


def get_course(course_id: int) -> Course | None:
    return db.session.get(Course, course_id)


def list_courses() -> list[Course]:
    return Course.query.order_by(Course.id.desc()).all()


def _active_turns_for_course(course: Course, session_ids: list[int]) -> list[Turn]:
    """这门课还有没有正在跑的任务（授课 / 图解 / 大纲都算）。

    评审 5.5：`delete_course()` 原来只拦"正在生成大纲"，没有拦教学与图解。
    于是可能出现：另一个窗口里这一轮还在讲，用户把课删了，
    后台线程继续往已删除的会话里写 Message、往已清空的目录里归档讲义——
    最后表现成"删除之后又冒出一条消息"或者一串文件系统错误。
    """
    query = Turn.query.filter(Turn.status.in_(("pending", "running")))
    if session_ids:
        query = query.filter(
            db.or_(
                Turn.course_id == course.id,
                Turn.session_id.in_(session_ids),
            )
        )
    else:
        query = query.filter(Turn.course_id == course.id)
    return query.all()


def delete_course(course_id: int) -> dict | None:
    """删除课程，连同它的会话、回合，以及本地讲义目录。

    返回摘要供界面提示（删掉了些什么、讲义目录是否清理成功）；课程不存在返回 None。

    **为什么不能只删 Course 靠级联**：
      `Course → Chapter → KnowledgePoint → LessonPlan` 这条链配了 cascade，能自动清；
      但 `StudySession` 只在模型上有 `course_id` 外键，Course 一侧**没有 relationship**，
      删课程不会碰它；`Turn.session_id` 现在虽然声明了 ForeignKey，
      但 SQLite 默认不启用外键约束，清理仍必须显式做。
      只删 Course 会留下一堆孤儿会话与回合。

    **ProfileEvent 故意保留**：它记的是"这位学习者的画像如何随学习变化"，
    属于全局，不属于任何一门课程。所以只把指向被删回合的 `turn_id` 置空，
    事件本身留着——把它一起删掉等于抹掉学习画像的来历。

    这一版修了三处（评审 3.2 / 5.5）：
        ① **拦所有活跃任务**，不只是"正在生成大纲"；
        ② `ProfileEvent.turn_id` 原来被拿去和**会话 id** 比较（字段语义错了），
           现在按真正的 turn id 清；
        ③ 任务按 `session_id` **或** `course_id` 删，大纲任务不再变成孤儿。
    """
    from . import lecture_notes  # 函数内导入：避开 services 包启动期的循环引用

    course = db.session.get(Course, course_id)
    if course is None:
        return None

    # 规划中不让删：后台线程正在往这门课里写章节，
    # 删掉它只会让那次规划以一个莫名其妙的错误收场（"课程不存在"）。
    # 等它结束（或等用户在界面上看到失败）再删，是更省事也更好理解的做法。
    if not can_delete_course(course):
        raise ValueError("这门课正在生成大纲，等它结束再删（或先关掉服务）")

    title = course.title
    session_ids = [
        row[0] for row in db.session.query(StudySession.id).filter_by(course_id=course_id).all()
    ]

    # ① 还有教学/图解在跑：明确拒绝，而不是删了之后让后台线程炸在半路
    running = _active_turns_for_course(course, session_ids)
    if running:
        kinds = "、".join(sorted({turn.kind for turn in running}))
        raise ValueError(f"这门课还有任务在跑（{kinds}），等它结束再删")

    # ③ 先把这门课的任务全部找出来（大纲任务挂在 course_id 上，不在会话里）
    turn_ids = [
        row[0]
        for row in db.session.query(Turn.id)
        .filter(db.or_(Turn.course_id == course.id,
                       Turn.session_id.in_(session_ids) if session_ids else False))
        .all()
    ]

    # ② 画像事件不删，只断开它指向的回合（按**真正的 turn id**）
    if turn_ids:
        for event in ProfileEvent.query.filter(ProfileEvent.turn_id.in_(turn_ids)).all():
            event.turn_id = None

    turns = 0
    if turn_ids:
        turns = Turn.query.filter(Turn.id.in_(turn_ids)).delete(synchronize_session=False)

    for session_id in session_ids:
        session = db.session.get(StudySession, session_id)
        if session is not None:
            db.session.delete(session)  # Message 靠 cascade 跟着走

    db.session.delete(course)  # Chapter → KnowledgePoint → LessonPlan 靠 cascade 跟着走
    db.session.commit()

    notes = lecture_notes.remove_course_dir(course_id=course_id, course_title=title)
    return {
        "title": title,
        "sessions": len(session_ids),
        "turns": turns,
        "notes_removed": notes["removed"],
        "notes_path": notes["path"],
        "notes_error": notes["error"],
    }


def local_syllabus_text(course: Course, point: KnowledgePoint | None, *, window: int = 3) -> str:
    """只给当前位置附近的上下文，而不是整份大纲。

    整份大纲每轮都往上下文里塞，是"上下文污染"的典型来源：
    课程越往后知识点越多，与当下无关的内容越占注意力预算。
    这里只给当前位置前后各 window 个，够看清"从哪来、到哪去"就行。
    """
    flat = [(ch, pt) for ch in course.chapters for pt in ch.points]
    if not flat:
        return ""

    index = 0
    if point is not None:
        for i, (_, item) in enumerate(flat):
            if item.id == point.id:
                index = i
                break

    start = max(0, index - window)
    end = min(len(flat), index + window + 1)

    lines = [
        f"整门课共 {len(course.chapters)} 章 / {len(flat)} 个知识点。"
        f"当前位置附近（前后各 {window} 个）："
    ]
    for i in range(start, end):
        chapter, item = flat[i]
        if i == index:
            lines.append(f"  ▶ {chapter.title} / {item.title}　← 现在在这里")
        else:
            mark = "（已讲）" if item.status == "done" else ""
            lines.append(f"    {chapter.title} / {item.title}{mark}")
    return "\n".join(lines)


def course_to_dict(course: Course) -> dict[str, Any]:
    outline = outline_state(course)
    return {
        "id": course.id,
        "title": course.title,
        "direction": course.direction,
        "focus": course.focus,
        "status": course.status,
        # 资料模式要带出去："继续完善"时要回填，课程页要显示它依据的是什么。
        "material_mode": course.material_mode,
        "material_path": course.material_path,
        # 大纲状态：开课页靠它决定"该去哪一步"——
        # 已经就绪的直接进课程，还在规划的显示进度而不是让用户再点一次。
        "outline_state": outline["state"],
        "outline_stage": outline["stage"],
        # 与列表接口共用同一个判断，避免两处说法不一致。
        "can_delete": can_delete_course(course),
        "chapters": [
            {
                "id": ch.id,
                "title": ch.title,
                "summary": ch.summary,
                "points": [
                    {
                        "id": pt.id,
                        "title": pt.title,
                        "summary": pt.summary,
                        "difficulty": pt.difficulty,
                        "est_minutes": pt.est_minutes,
                        "status": pt.status,
                        # 资料出处。空列表是有意义的：表示这个点由模型补充，
                        # 学生据此判断"要不要回原文核对"。
                        "sources": json.loads(pt.sources or "[]"),
                    }
                    for pt in ch.points
                ],
            }
            for ch in course.chapters
        ],
    }


# ---------------- 大纲状态 ----------------

# 课程列表要显示的四种情况。
# "课程自己的 status"（draft / ready）不够用：draft 既可能是"还没点过生成"，
# 也可能是"上次生成失败了"——用户在这两种情况下的处境完全不同，
# 前者该去点生成，后者该知道为什么失败、再来一次。
OUTLINE_IDLE = "idle"        # 没规划过
OUTLINE_RUNNING = "running"  # 正在规划
OUTLINE_FAILED = "failed"    # 上次失败了
OUTLINE_READY = "ready"      # 已就绪


def latest_outline_turn(course_id: int) -> Turn | None:
    """这门课最近一次大纲生成。"""
    return (
        Turn.query
        .filter_by(course_id=course_id, kind="outline")
        .order_by(Turn.id.desc())
        .first()
    )


def outline_state(course: Course) -> dict[str, str]:
    """这门课现在的大纲状态、阶段文字、以及上次的错误。

    阶段文字来自 `Turn.stage`——由 runtime 在每次 status 事件时写回数据库。
    这是**离开页面也能看见进度**的全部依据：那条 SSE 早就断了。
    """
    turn = latest_outline_turn(course.id)
    if turn is None:
        return {"state": OUTLINE_IDLE, "stage": "", "error": ""}

    if turn.status in ("pending", "running"):
        return {"state": OUTLINE_RUNNING, "stage": turn.stage or "", "error": ""}
    if turn.status == "failed":
        return {"state": OUTLINE_FAILED, "stage": "", "error": turn.error or ""}
    return {
        "state": OUTLINE_READY if course.status == "ready" else OUTLINE_IDLE,
        "stage": "",
        "error": "",
    }


def can_delete_course(course: Course) -> bool:
    """现在能不能删这门课。

    **单一来源**：列表接口、详情接口、以及删除动作本身，都由这里回答。
    分开各写一遍迟早会自相矛盾——"列表说不能删、详情说能删"这个不一致
    就是被端到端测试抓出来的（详情接口当时压根没有这个字段）。
    """
    return outline_state(course)["state"] != OUTLINE_RUNNING


def course_overview(course: Course) -> dict:
    """课程列表用的轻量统计。

    工作台上的进度数字必须**来自真实数据**，不能是估计值或装饰。
    """
    total = sum(len(ch.points) for ch in course.chapters)
    done = sum(1 for ch in course.chapters for pt in ch.points if pt.status == "done")
    outline = outline_state(course)
    return {
        "id": course.id,
        "title": course.title,
        "direction": course.direction,
        "focus": course.focus,
        "status": course.status,
        # 工作台要能一眼区分"依据资料"的课程：卡片的徽标靠它。
        "material_mode": course.material_mode,
        "material_path": course.material_path,
        # 在不在规划、跑到哪一步、上次为什么失败——以及要不要拦着不让删。
        "outline_state": outline["state"],
        "outline_stage": outline["stage"],
        "outline_error": outline["error"],
        "can_delete": can_delete_course(course),
        "chapters": len(course.chapters),
        "total_points": total,
        "done_points": done,
    }


# ---------------- 进度 ----------------

def first_pending(course: Course) -> tuple[Chapter | None, KnowledgePoint | None]:
    for ch in course.chapters:
        for pt in ch.points:
            if pt.status != "done":
                return ch, pt
    return None, None


def point_position(course: Course, point_id: int) -> tuple[int, int]:
    """知识点在课程里的位置：(章序号, 知识点序号)，都从 1 开始；找不到返回 (0, 0)。

    讲义文件名要用它，保证同一个知识点每次都落到同一个文件里（多轮讲解累积）。
    """
    for chapter_index, chapter in enumerate(course.chapters, start=1):
        for point_index, point in enumerate(chapter.points, start=1):
            if point.id == point_id:
                return chapter_index, point_index
    return 0, 0


def next_point(course: Course, after_point_id: int) -> tuple[Chapter | None, KnowledgePoint | None]:
    """按大纲顺序取 after_point_id 之后的那个知识点。"""
    flat: list[tuple[Chapter, KnowledgePoint]] = [
        (ch, pt) for ch in course.chapters for pt in ch.points
    ]
    for idx, (ch, pt) in enumerate(flat):
        if pt.id == after_point_id:
            if idx + 1 < len(flat):
                return flat[idx + 1]
            return None, None
    return first_pending(course)


# ---------------- 大纲生成（异步） ----------------

def _outline_from_materials(course: Course, publish: Callable[[dict], None]) -> list[dict]:
    """资料模式的编排链路：解析 → 逐份摘要 → 依资料排章节。

    **每一步都 publish 进度**：这条链路要读盘、还要对每份材料过一次模型，
    资料多时可能好几分钟。没有进度用户只能干等，会以为卡死了——
    而"请耐心等待"这个承诺，只能靠看得见的进度来兑现。

    失败一律抛 ValueError，交给 runtime 转成 alert 显示给用户：
    资料路径不对、一份都读不出文字，这些都必须让人知道，
    否则用户会以为"系统读了资料，只是读得不认真"。
    """
    from ..agents.material_planner import generate_outline_from_materials
    from . import materials_service, profile_service

    # 重新生成大纲 = 用户显式要求"按我现在的资料重来"。
    # 这时必须丢掉讲师的解析缓存：他刚刚改过课件，不该让上一次读到的内容挡住
    # （新增/删除文件本来就会让指纹失效，这一句是为了"同一天同一份被替换过"的情况）。
    materials_service.clear_report_cache()

    folder = Path((course.material_path or "").strip())
    if not (course.material_path or "").strip():
        raise ValueError("这门课选了资料模式，但没有填资料文件夹")
    if not folder.is_dir():
        raise ValueError(
            f"资料文件夹不存在或不是一个目录：{folder}\n"
            "请检查路径（例如 D:\\课件\\高等数学），改好之后重新生成大纲。"
        )

    publish({"type": "status", "status": "正在读取资料文件夹"})
    # 走带缓存的入口：大纲这一步解析过后，讲师后面每一轮取原文都能直接命中缓存，
    # 不必把几十份课件重新解析一遍（见 materials_service.load_reports）。
    reports = materials_service.load_reports(folder)
    if not reports:
        raise ValueError(
            f"这个文件夹里没有找到可读的资料：{folder}\n"
            "支持的格式：PDF、PPTX、DOCX、TXT、MD。"
        )

    stats = materials_service.summarize_reports(reports)
    if stats["parsed"] == 0:
        names = "、".join(stats["failed_names"] + stats["no_text_names"])
        raise ValueError(
            f"资料都没能读出文字：{names}\n"
            "目前只支持能提取文字的 PDF / PPTX / DOCX / TXT / MD。\n"
            "如果它们是扫描件或整页图片，建议先去 DeepSeek 官网或其它能读图的 AI 平台，"
            "把图片整理成 Word / Markdown 文档再放回文件夹——"
            "那边能直接读图，你还能核对改错；这里不支持图片，硬来也认得不准。"
        )

    publish({
        "type": "status",
        "status": f"读到 {stats['parsed']} 份资料，正在逐份理解",
    })
    digests, failures = materials_service.summarize_materials(
        reports,
        on_progress=lambda i, n, name: publish({
            "type": "status", "status": f"读资料 {i}/{n}：{name}",
        }),
    )
    if not digests:
        raise ValueError("资料一份都没能读懂，无法据它编排章节。")

    if failures:
        # 不阻断，但要如实说：用户有权知道有哪几份没被参考到。
        publish({
            "type": "warn",
            "message": f"有 {len(failures)} 份资料没读进去，编排时不会参考它们："
                       + "；".join(failures[:3]),
        })

    publish({"type": "status", "status": "正在按资料编排章节"})
    return generate_outline_from_materials(
        digest=materials_service.render_digests(digests, failures),
        profile_summary=profile_service.summary_text(),
        focus=course.focus,
        mode=course.material_mode,
    )


def _archive_sessions(course: Course) -> int:
    """重新生成大纲前，把"进行中"的会话归档。返回归档了几个。

    为什么不直接删：历史对话是学习记录，删了就再也找不回来。
    为什么必须归档（评审 3.2）：
        · 旧会话的 `current_point_id` 指向**即将被删除**的知识点，留着就是悬空指针；
        · 旧消息属于旧大纲。新大纲的消息如果写进同一条会话，时间轴里会同时出现
          两套章节结构，用户根本分不清哪句是"在讲什么"。
    归档之后 `create_session()` 找不到 active 会话，会新建一个——
    旧会话仍保存在库里（可在讲义/历史里查），但不再被自动复用。
    """
    archived = 0
    active = StudySession.query.filter_by(course_id=course.id, status="active").all()
    for session in active:
        session.status = "archived"
        session.current_point_id = None
        session.current_chapter_id = None
        archived += 1
    return archived


def make_outline_worker(course_id: int) -> Callable[[Callable[[dict], None]], str]:
    """构造线程池里执行的大纲生成任务。

    注意：worker 跑在别的线程，不能复用请求线程里的 ORM 对象，
    所以只传 id，进来重新查。
    """

    def worker(publish: Callable[[dict], None]) -> str:
        from ..agents.planner import generate_outline
        from . import profile_service

        course = get_course(course_id)
        if course is None:
            raise ValueError("课程不存在")

        publish({"type": "status", "status": "规划中"})

        if uses_materials(course.material_mode):
            # 资料模式：知识来源是用户给的那些文件，而不是模型的先验。
            # 这条链路要读盘 + 逐份过模型，可能好几分钟，所以每一步都报进度。
            chapters = _outline_from_materials(course, publish)
        else:
            chapters = generate_outline(
                direction=course.direction,
                focus=course.focus,
                profile_summary=profile_service.summary_text(),
            )

        # 重新生成时清掉旧大纲（章节删除会级联删知识点）。
        #
        # 但在删之前必须先把旧会话归档：否则会话里的 current_point_id 变成
        # 悬空指针，而旧消息会与新大纲的章节混在同一条时间轴里（评审 3.2）。
        # 会话**不删**——那是学习记录，删了就找不回来。
        archived = _archive_sessions(course)
        if archived:
            publish({
                "type": "status",
                "status": f"已归档 {archived} 个旧会话，开始生成新大纲",
            })
        course.outline_version = (course.outline_version or 0) + 1

        for ch in list(course.chapters):
            db.session.delete(ch)
        db.session.flush()

        for i, ch_data in enumerate(chapters):
            chapter = Chapter(
                course_id=course.id,
                title=ch_data["title"],
                summary=ch_data["summary"],
                order_index=i,
            )
            db.session.add(chapter)
            db.session.flush()
            for j, pt in enumerate(ch_data["points"]):
                db.session.add(KnowledgePoint(
                    chapter_id=chapter.id,
                    title=pt["title"],
                    summary=pt["summary"],
                    difficulty=pt["difficulty"],
                    est_minutes=pt["est_minutes"],
                    # 资料模式下是"来自哪份材料的哪几页"；无资料时为空
                    #（原有的自由编排不产出这个字段）。
                    sources=json.dumps(pt.get("sources") or [], ensure_ascii=False),
                    order_index=j,
                ))

        course.status = "ready"
        db.session.commit()

        total_points = sum(len(ch.points) for ch in course.chapters)
        summary = f"已生成 {len(course.chapters)} 章 / {total_points} 个知识点"
        publish({
            "type": "outline",
            "course_id": course.id,
            "summary": summary,
            # 把生成好的大纲**直接带在事件里**：前端据此渲染，不必再发一次 GET。
            # 少一次往返，也彻底避开"事件到了、后续查询却拿到旧数据"这类时序问题。
            "outline": course_to_dict(course),
        })
        return summary

    return worker


# ---------------- 学习会话 ----------------

def create_session(course_id: int) -> StudySession:
    course = get_course(course_id)
    if course is None:
        raise ValueError("课程不存在")

    # 复用该课程进行中的会话。
    # 课程页每次进入都会调这里，如果每次都新建，对话历史就没了，
    # 也不符合"一门课一个进行中的学习会话"这个直觉。
    existing = (
        StudySession.query
        .filter_by(course_id=course.id, status="active")
        .order_by(StudySession.id.desc())
        .first()
    )
    if existing is not None:
        return existing

    chapter, point = first_pending(course)
    session = StudySession(
        course_id=course.id,
        current_chapter_id=chapter.id if chapter else None,
        current_point_id=point.id if point else None,
    )
    db.session.add(session)
    db.session.commit()
    return session


def get_session(session_id: int) -> StudySession | None:
    return db.session.get(StudySession, session_id)
