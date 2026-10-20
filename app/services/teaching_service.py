"""教学服务：装配上下文 → 备课 → 流式授课 → 落库 → 归档 → 复盘。

一次教学回合 = 一个 Turn，顺序是：
    ① 判断要不要翻页（学习者说"下一节"；或教案已讲完、学习者又说"继续"）
    ② 备课——首次走到这个知识点时才做，教案落在数据库里（**不进上下文**）
    ③ 装配：教案视图 + 已讲要点 + 局部大纲 + 画像 + 最近对话
    ④ 讲师流式产出，逐段经 SSE 推给前端
    ⑤ 落库 + 推送进度（左栏据此更新节点状态与高亮）
    ⑥ 讲义归档到本地工作区
    ⑦ 后台线程做回合复盘（把这一轮压成"已讲要点"、更新讲授进度）

分层约定：本模块只做**编排**，不写 SQL 细节、不写提示词。
    智能体 → agents/    提示词 → prompts/    纯能力（文件读写、复盘细节）→ 各自的 service
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from flask import current_app

from ..extensions import db
from ..materials import uses_materials
from ..models import KnowledgePoint, LessonPlan, Message, StudySession
from . import (
    curriculum_service,
    lecture_notes,
    lesson_service,
    materials_service,
    page_service,
    profile_service,
)
from .runtime import current_turn_id


def _runtime():
    """拿到任务运行时。

    为什么从 `current_app.extensions` 取而不是 import 一个全局单例：
    运行时是**每个应用实例一个**（测试里每个用例都会新建一个 app），
    做成模块级单例会让测试之间互相污染（上一条用例的线程池与订阅表还在）。
    """
    return current_app.extensions["turn_runtime"]


# ---------------- 上下文装配 ----------------

def recent_message_limit() -> int:
    """每回合装配进提示词的"最近消息"条数。

    配置项 `CONTEXT_RECENT_TURNS` 说的是**轮数**（1 轮 = 学习者提问 + 讲师回复各一条），
    这里换算成消息条数。设下限 2 是为了至少保留一问一答，避免配成 0 后讲师失去上下文。
    """
    turns = int(current_app.config.get("CONTEXT_RECENT_TURNS", 6))
    return max(2, turns * 2)


def history_text(session: StudySession, limit: int) -> str:
    """最近若干轮对话，压成文本供提示词使用。"""
    messages = (
        Message.query.filter_by(session_id=session.id)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    if not messages:
        return ""
    lines = []
    for msg in reversed(messages):
        who = "学习者" if msg.role == "user" else "我"
        content = (msg.content or "").strip()
        if len(content) > 800:
            content = content[:800] + "……"
        lines.append(f"{who}：{content}")
    return "\n\n".join(lines)


def progress_snapshot(session: StudySession) -> dict:
    """左栏需要的进度快照。

    随每轮结束推给前端，左栏据此更新节点状态与高亮——
    否则"讲到哪了"只体现在右侧对话里，左边那棵树是死的。
    """
    return {
        "session_id": session.id,
        "current_chapter_id": session.current_chapter_id,
        "current_point_id": session.current_point_id,
        "points": [
            {"id": point.id, "chapter_id": chapter.id, "status": point.status}
            for chapter in session.course.chapters
            for point in chapter.points
        ],
        # 教案进度（第几步/共几步），前端据此显示"备课到哪了"
        "plan": lesson_service.to_dict(lesson_service.get_plan(session.current_point_id))
        if session.current_point_id
        else {},
        # "系统正在怎么适应你"（给学生看的一句话）。
        # 放在进度快照里，是因为它每轮都可能变（后台线程刚改完 pace，下一轮就该显示出来），
        # 而进度事件恰好每轮都推——不必让前端为此再多一次轮询。
        "adaptation": profile_service.adaptation_note(),
    }


def current_context(session: StudySession) -> dict:
    """当前所在的章节 / 知识点。"""
    course = session.course
    chapter = point = None
    if session.current_point_id:
        point = db.session.get(KnowledgePoint, session.current_point_id)
        if point:
            chapter = point.chapter
    if point is None:
        chapter, point = curriculum_service.first_pending(course)
    return {"course": course, "chapter": chapter, "point": point}


# ---------------- 资料装配（按需取用） ----------------

def _material_query(plan: LessonPlan | None, point: KnowledgePoint, user_message: str) -> str:
    """这一轮的语境：现在要讲的那一步在讲什么 + 学生刚问的话。

    它就是"按需"里的那个"需"——同一个知识点，讲第 1 步和第 3 步，
    从资料里挑出来的原文可以不是同一页。
    """
    parts = [point.title, point.summary or "", user_message or ""]
    steps = plan.steps_list() if plan is not None else []
    if steps:
        # 教案全部讲完后学生仍在追问时，current_step 会指到 steps 之外，这里夹回来
        index = max(0, min(plan.current_step, len(steps) - 1))
        current = steps[index]
        parts.append(str(current.get("title") or ""))
        parts.append(str(current.get("intent") or ""))
        parts.extend(str(item) for item in current.get("key_points") or [])
    return "\n".join(part for part in parts if part)


def material_view_for(
    course, point: KnowledgePoint | None, plan: LessonPlan | None, user_message: str
) -> materials_service.MaterialView:
    """这一轮要依据的资料原文与出处——**按需取用**，而不是把整批资料塞进上下文。

    "按需"由三件事共同决定：

      ① **范围按需**：只取这个知识点 `sources` 标出的位置（编排阶段已经对回了页码），
         不是整批资料——一门课的课件常有几十份，其中九成与这一步无关；
      ② **内容按需**：在候选块里按"这一步要讲的要点 + 学生这一轮的话"挑最相关的几块
         （`materials_service.select_blocks`），所以讲义里讲到的原话、例子、定义
         会在真正用得着的那一轮出现，而不是每轮都端上去；
      ③ **取不到就直说**：文件夹被移走、路径写错、资料是扫描件——
         都只是"这一轮没有原文可依据"，绝不能挡住讲课
         （与备课失败同一个原则：辅助的东西坏了，课还得能上）。

    没有教案（备课失败）也照样取：那时就用消息与知识点本身当语境，
    比"因为没教案所以连资料也不看"合理。
    """
    empty = materials_service.MaterialView(note="", labels=[])

    if point is None or not uses_materials(course.material_mode):
        return empty

    path_text = (course.material_path or "").strip()
    if not path_text:
        # 选了资料模式却没填路径：与"没有资料"同一处理，同样不挡讲课
        return empty

    try:
        sources = json.loads(point.sources or "[]")
    except json.JSONDecodeError:
        sources = []

    # 这门课明明指定了资料，却一份都读不出来（文件夹被移走、路径写错、整批是扫描件）：
    # 这时要告诉讲师的是"没找到原文"，而不是"你没有资料"——
    # 后者会让他放心大胆地按自己的知识讲，还讲得像照资料讲的一样。
    nothing_readable = materials_service.MaterialView(
        note=materials_service.NO_PASSAGE, labels=[]
    )

    try:
        reports = materials_service.load_reports(Path(path_text))
    except Exception:  # noqa: BLE001 —— 读盘失败只意味着这一轮没有原文
        current_app.logger.exception("资料读取失败，这一轮按无资料讲授")
        return nothing_readable

    if not any(report.ok and report.has_text for report in reports):
        return nothing_readable

    return materials_service.build_material_view(
        reports,
        sources,
        course.material_mode,
        query=_material_query(plan, point, user_message),
    )


# ---------------- 推进意图 ----------------

# 学习者明确要求"往下走"的说法。
#
# 为什么需要这个：讲师讲哪个知识点，是由 session.current_point 决定的，
# 而用户说"下一节"**根本不会改变它**——于是人会一直被困在同一个知识点里，
# 讲师只会一遍遍重讲（实测出现过 14 轮都停在同一个点的情形）。
# 这里给一句人话一个出口。
_NEXT_PATTERNS = (
    "下一节", "下一个", "接着下", "继续往下", "往下讲", "往下走",
    "跳过", "换个知识点", "换一个知识点", "讲别的", "不用讲这个",
)


def wants_next(message: str) -> bool:
    """学习者是否明确要求推进到下一个知识点。"""
    text = (message or "").strip()
    return bool(text) and any(pattern in text for pattern in _NEXT_PATTERNS)


# 比 wants_next 更宽的说法，**只在教案已经讲完时**才启用。
# 那时"继续"除了往下走没有别的意思；而如果当前步骤还没讲完，
# "继续"就只是"接着讲这一步"，绝不能当成翻页。
_FORWARD_PATTERNS = ("继续", "接着", "往下", "下一个", "懂了", "会了", "明白了", "不用了")

# 简短的应声。**中英文和口语都要收**：
# 实测学习者经常只回一个 "OK"，而它哪个中文模式都不匹配——教案早已讲完，
# 却因为这一句卡在同一个知识点里出不去（讲义里出现过连续几轮讲同一批内容）。
# 两边的代价不对称：误判只是讲完时多翻一页（还能退回来），漏判是直接卡死。
_SHORT_AGREEMENTS = (
    "好", "嗯", "可以", "行", "懂了", "会了", "明白了",
    "ok", "okay", "yes", "yep", "没问题", "好嘞", "行吧", "知道了",
)
# 上面这些词要求整条消息**很短**才算，否则"这个方法行不通"也会被读成同意
_AGREEMENT_MAX_LEN = 6


def wants_forward(message: str) -> bool:
    """学习者是否在表达"往下走"（比 wants_next 宽，含简短的应声）。"""
    text = (message or "").strip()
    if not text:
        return False
    if wants_next(text):
        return True
    # 大小写和尾部标点都不该改变意思："OK"、"ok!"、"Ok。" 是同一句话
    lowered = text.lower().strip(" \t!.。！?？~")
    if len(lowered) <= _AGREEMENT_MAX_LEN and any(w in lowered for w in _SHORT_AGREEMENTS):
        return True
    return any(pattern in text for pattern in _FORWARD_PATTERNS)


# ---------------- 教学回合 ----------------

def make_teach_worker(session_id: int, user_message: str, *,
                      advance_step: bool = False) -> Callable[[Callable[[dict], None]], str]:
    """构造教学回合。

    dvance_step=True 表示"这一轮是接着往下讲"（「继续讲」或课堂里的反馈），
    开讲前把教案推进到下一步；普通提问/答疑必须保持 False，否则每问一句就翻一页。
    """

    def worker(publish: Callable[[dict], None]) -> str:
        """外壳：**会话互斥** + 等上一轮复盘，真正的内容在 `_teach` 里。

        为什么需要这一层（评审点出的 2.3）：
            同一会话原先可以被两轮教学同时推进——两轮都读 `LessonPlan.current_step`、
            都改 `covered`、都写 `Message`，结果是进度互相覆盖、讲义里出现重复段。
            而且回合后的复盘跑在一条自开的 daemon 线程里，根本不在线程池的
            并发上限之内。

        顺序很重要：
            ① 先 `wait_for_side()`——复盘会更新教案进度，下一轮要用新进度；
            ② 再拿会话锁——派生工作自己也要拿这把锁，**拿着锁等它是死锁**。
        """
        runtime = _runtime()
        # 先报一句状态再等：上一轮复盘可能还要几十秒，
        # 界面（以及停滞看门狗）都该知道"这一轮在排队，不是卡死"。
        publish({"type": "status", "status": "等待上一步收尾"})
        if not runtime.wait_for_side(session_id):
            # 超时不等于失败：宁可带着旧进度开讲，也不能把课卡住
            publish({"type": "warn", "message": "上一轮的复盘还没结束，这一轮按当前进度继续"})
        with runtime.session_lock(session_id):
            return _teach(publish)

    def _teach(publish: Callable[[dict], None]) -> str:
        from ..agents.lecturer import teach_stream

        session = db.session.get(StudySession, session_id)
        if session is None:
            raise ValueError("学习会话不存在")

        def publish_progress() -> None:
            """把最新进度推给前端——左栏的节点状态与高亮靠它更新。"""
            publish({"type": "progress", "progress": progress_snapshot(session)})

        # 该不该往前翻？两种情况：
        #   ① 学习者明确说"下一节/跳过"；
        #   ② 这个知识点的教案**已经讲完**，而学习者又说"继续/懂了"——
        #      那就该翻到下一个知识点，而不是把同一批内容反复组装成题。
        # （② 是"讲完了却出不去"的直接解药：实测出现过最后几轮一直在演练、
        #   迟迟不进入下一个知识点的情形。）
        ctx = current_context(session)
        point_before = ctx["point"]
        plan_before = lesson_service.get_plan(point_before.id) if point_before else None
        lesson_done = plan_before is not None and plan_before.is_finished()

        if wants_next(user_message) or (lesson_done and wants_forward(user_message)):
            advance_point(session.id)
            publish_progress()

        ctx = current_context(session)
        course, chapter, point = ctx["course"], ctx["chapter"], ctx["point"]

        publish({"type": "status", "status": "备课中"})

        # 备课：第一次走到这个知识点时，先想清楚"这一节分几步、每步要什么"。
        # 教案落在数据库里（也就是上下文之外），每轮只把当下需要的那一份拉回来——
        # 这就是 Anthropic 说的 structured note-taking：计划在外部，上下文只放当下要用的。
        plan = None
        if point is not None:
            try:
                plan = lesson_service.ensure_plan(
                    point,
                    profile_summary=profile_service.summary_text(),
                    course_title=course.title,
                    chapter_title=chapter.title if chapter else "",
                )
            except Exception as exc:  # noqa: BLE001
                # 备课失败不能挡住讲课：没有教案也讲得下去，只是没那么有条理
                plan = None
                publish({"type": "warn", "message": f"备课没成功，这一轮按无教案讲授：{exc}"})
            publish_progress()

        # 翻到下一步：**显式**由调用方要求（"继续讲" / 课堂里的反馈），
        # 不再由回合后的复盘模型猜（原来的做法每轮多一次调用，而且经常判不准）。
        # 只在"当前这一步已经有过页"时前进：第一页还在同一节里，不该翻。
        if (
            advance_step
            and plan is not None
            and point is not None
            and not plan.is_finished()
            and any(one.step_index == plan.current_step
                    for one in page_service.pages_for(session.id, point.id))
        ):
            plan.current_step = min(plan.current_step + 1, len(plan.steps_list()))
            plan.step_rounds = 0
            db.session.commit()
            publish_progress()

        # 这一轮要依据的资料：**按需取**——只取这个知识点 sources 标出的位置，
        # 再按"现在讲第几步 + 学生刚问什么"精选。首次会读盘，之后走解析缓存。
        if uses_materials(course.material_mode):
            publish({"type": "status", "status": "翻资料中"})
        material = material_view_for(course, point, plan, user_message)

        # 严格模式下"声明了来源却核验不到原文"必须**当场**告诉用户。
        # 用 alert 而不是 warn：warn 几秒后自己飘走，而这件事关系到
        # "这一轮讲的到底有没有资料依据"——它得留在气泡上让人看见。
        if getattr(material, "strict_miss", False):
            publish({
                "type": "alert",
                "message": "严格模式：这一轮没找到能核验的资料原文，"
                           "讲师会先说明再问你是否允许补充。",
            })

        # 这一轮**真正用到**的资料出处：随流推一条，学生当场就能看到
        # "讲的是我资料里的第几页"，不必等刷新后再去看消息标签。
        if material.labels:
            publish({"type": "sources", "labels": material.labels})

        publish({"type": "status", "status": "讲课中"})

        buffer: list[str] = []
        for delta in teach_stream(
            course_title=course.title,
            chapter_title=chapter.title if chapter else "",
            point_title=point.title if point else "",
            point_summary=point.summary if point else "",
            plan_brief=lesson_service.plan_brief(plan) if plan else "",
            covered=plan.covered if plan else "",
            syllabus_text=curriculum_service.local_syllabus_text(course, point),
            profile_summary=profile_service.summary_text(),
            history_text=history_text(session, limit=recent_message_limit()),
            user_message=user_message,
            materials_note=material.note,
        ):
            buffer.append(delta)
            publish({"type": "delta", "text": delta})

        full_text = "".join(buffer)

        tags = []
        if chapter:
            tags.append(f"章节：{chapter.title}")
        if point:
            tags.append(f"知识点：{point.title}")
            if point.status == "pending":
                point.status = "teaching"
        # 这一轮**真正用到**的资料出处（不是"这门课有资料"）。
        # 放在标签里，学生不用读正文就能核对"讲的是不是我那份资料"——
        # 这正是选严格模式的人最在意的一件事。
        tags.extend(f"依据：{label}" for label in material.labels)

        message = Message(
            session_id=session.id,
            role="agent",
            agent="lecturer",
            content=full_text,
            tags=json.dumps(tags, ensure_ascii=False),
            # 关联三元组：这是"这条讲解属于哪门课/哪个知识点/哪一轮"的
            # **唯一可靠来源**。以前只有 tags 里的文本，切换知识点后
            # 图解会拿错素材，历史分组也会把问答拆开。
            course_id=course.id,
            point_id=point.id if point else None,
            turn_id=current_turn_id(),
        )
        db.session.add(message)
        db.session.commit()

        # 卡片课堂（P0）：把这一轮讲授切成页。
        #
        # 分页是**确定性**的（见 `page_builder`），因为讨论时定的是"先能简单实现"：
        # 讲师仍按原来的方式生成整段内容，超长自动分页。这样提示词不用改，
        # 讲授质量不会因为"按页限制篇幅"而退化。
        #
        # 落页失败绝不能连累授课：学生已经看到全文了，卡片只是另一种呈现方式。
        try:
            pages = page_service.record_pages(
                session=session,
                point=point,
                plan=plan,
                content=full_text,
                materials=material.labels,
                turn_id=current_turn_id(),
            )
            if pages:
                # 这条消息归属它的第一页：页内答疑、历史分组都靠它
                message.page_id = pages[0].id
                # covered 用**页标题**确定性累积：不再依赖复盘模型（它现在只在整节结束时跑）
                _append_covered(plan, pages)
                db.session.commit()
                publish({
                    "type": "page",
                    "point_id": point.id if point else None,
                    "count": len(pages),
                    "page": pages[-1].to_dict(),
                })
        except Exception:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.exception("把讲授切成页失败（这一轮仍算讲完了）")

        # 这一轮可能让某个知识点从 pending 变成 teaching，进度要跟着更新
        publish_progress()

        # 自动落盘：讲义的价值在于"以后还能翻"，所以不依赖用户手动导出
        _archive_to_lecture(
            publish, course=course, chapter=chapter, point=point, content=full_text
        )

        # 回合后处理：复盘（讲授进度）+ 观察（学习信号），都放后台线程。
        # 产物只供**下一轮**使用，不该让用户为 done 事件多等几秒。
        #
        # 注意这里只要求 point 存在、不要求 plan 存在：
        # 备课失败（plan is None）时仍然要观察学生的反馈，
        # 否则"这节课没有教案"就会连带让自适应闭环也失效。
        if point is not None:
            # 整节讲完 = 复盘时机（原来每轮都复盘，用户提议改成整节一次）。
            # `reviewed_at` 保证只做一次：每次进课堂都再花一次模型调用没有意义。
            if plan is not None and plan.is_finished() and plan.reviewed_at is None:
                _schedule_point_review(
                    session_id=session.id,
                    plan_id=plan.id,
                    course_title=course.title,
                    chapter_title=chapter.title if chapter else "",
                    point_title=point.title,
                )

            _schedule_after_turn(
                session_id=session.id,
                point_title=point.title,
                user_message=user_message,
            )

        return full_text

    return worker


def _archive_to_lecture(
    publish: Callable[[dict], None],
    *,
    course,
    chapter,
    point,
    content: str,
    as_diagram: bool = False,
) -> None:
    """把这一轮的内容归档进该知识点的讲义文件。

    讲师的讲解与图解共用这一条路径——差别只是"追加一段正文"还是"追加一张 mermaid 图"。
    抽出来的理由很直接：这两处原先各写了一遍，包含同样的定位、同样的 try/except、
    同样的 saved 事件，改一处很容易忘了另一处。

    归档是**附加价值**：路径非法、磁盘满、权限不足都不该毁掉这一轮授课，
    所以这里故意捕获所有异常（实测除 OSError 外还会抛 ValueError，例如路径含空字符）。
    """
    if point is None or not (content or "").strip():
        return

    chapter_index, point_index = curriculum_service.point_position(course, point.id)
    try:
        if as_diagram:
            saved = lecture_notes.append_diagram(
                course_id=course.id,
                course_title=course.title,
                chapter_title=chapter.title if chapter else "",
                point_title=point.title,
                mermaid_code=content,
                chapter_index=chapter_index,
                point_index=point_index,
            )
        else:
            saved = lecture_notes.save_lecture(
                course_id=course.id,
                course_title=course.title,
                chapter_title=chapter.title if chapter else "",
                point_title=point.title,
                content=content,
                chapter_index=chapter_index,
                point_index=point_index,
            )
        if saved:
            publish({
                "type": "saved",
                "path": saved,
                "kind": "diagram" if as_diagram else "lecture",
            })
    except Exception as exc:  # noqa: BLE001
        label = "图解" if as_diagram else "讲义"
        # 这里用 alert 而不是 warn：这是"这一轮的产物没落盘"，
        # 而普通提示几秒钟就自己飘走了——用户回过神来已经不知道发生过什么，
        # 只会觉得"我的笔记怎么不见了"。alert 会留在气泡上，不自动消失。
        publish({"type": "alert", "message": f"{label}保存失败：{exc}"})


def _append_covered(plan: LessonPlan | None, pages: list) -> None:
    """把这一批页的标题累积进 `covered`（"已经讲过什么"）。

    以前这件事由逐轮复盘的模型做（`review_turn` 的 `covered_add`）。复盘改成
    整节一次之后，逐轮再问一次模型不值得——页标题本来就已经是"讲了什么"的摘要。
    代价是它比模型摘要粗一档，但**免费、确定、不会跑偏**。
    """
    if plan is None or not pages:
        return
    existing = plan.covered or ""
    lines: list[str] = []
    for page in pages:
        title = (page.title or "").strip()
        if not title:
            continue
        entry = f"- {title[:60]}"
        if entry not in existing and entry not in lines:
            lines.append(entry)
    if lines:
        plan.covered = "\n".join([existing] + lines).strip() if existing else "\n".join(lines)


def _schedule_point_review(
    *,
    session_id: int,
    plan_id: int,
    course_title: str,
    chapter_title: str,
    point_title: str,
) -> None:
    """整节复盘：把这一节的页汇总起来，做**一次**模型复盘。

    为什么放在侧线程池：它是一次模型调用，产物只供小测与下一个知识点使用，
    没有理由让学生为 done 事件多等几秒。互斥、计数、失败隔离都复用
    `submit_side` / `session_lock` 那一套（见 `_schedule_after_turn` 的说明）。
    """
    app = current_app._get_current_object()

    def run() -> None:
        from ..models import LessonPage, LessonPlan

        plan = db.session.get(LessonPlan, plan_id)
        if plan is None or plan.reviewed_at is not None:
            return
        pages = (
            LessonPage.query
            .filter_by(session_id=session_id, point_id=plan.point_id)
            .order_by(LessonPage.index)
            .all()
        )
        text = "\n\n".join(
            f"[第 {page.index + 1} 页] {page.title}\n{(page.content or '')[:600]}"
            for page in pages
        )
        lesson_service.review_point(
            plan=plan,
            course_title=course_title,
            chapter_title=chapter_title,
            point_title=point_title,
            pages_text=text,
        )

    _runtime().submit_side(app, run, scope=session_id, label="point-review")


def _schedule_after_turn(
    *,
    session_id: int,
    point_title: str,
    user_message: str,
) -> None:
    """回合后处理：**观察学习信号**，交给有界的侧线程池。

    复盘（"讲到第几步、新讲了什么"）已经不在这里了——它改成**整节讲完时做一次**
    （见 _schedule_point_review）。改的理由：逐轮复盘每轮多一次模型调用，
    而且它的问题在一节之内本来就模糊，模型经常判不准；整节再看，才回答得了
    "哪一步讲虚了"这种真正有用的问题。

    **不再自开 daemon 线程**（评审 2.3）：那条路径完全绕过了线程池的并发上限，
    用户快速连发几轮就会同时有好几个复盘线程在改教案。现在走
    `runtime.submit_side()`，容量 1、天然串行，并且会计入
    "这个会话还有没有派生工作没做完"——下一轮教学会等它。
    """
    app = current_app._get_current_object()

    def run() -> None:
        runtime = _runtime()
        try:
            # 复盘与下一轮教学要互斥：它读改写的是同一份 LessonPlan
            with runtime.session_lock(session_id):
                # 观察单独 try：复盘失败不该阻止观察，观察失败也不该影响复盘的结果
                try:
                    profile_service.observe_turn(
                        # 调度层目前不把 turn_id 下发到 worker（Turn 行是在 worker 构造之后才创建的），
                        # 所以这里传 None。画像变化的定位靠 created_at + signals 里的知识点，
                        # 已经够研究报告用；参数位留着，将来接上无需改签名。
                        turn_id=None,
                        point_title=point_title,
                        user_message=user_message,
                    )
                except Exception:  # noqa: BLE001
                    # 观察失败只影响"下一轮知不知道学生怎么了"，同样不该连累授课
                    app.logger.exception("学习信号观察失败")
        finally:
            db.session.remove()

    _runtime().submit_side(app, run, scope=session_id, label="after-turn")


def _lecturer_message_for_point(session_id: int, point, point_title: str):
    """取"**当前知识点**的最近一条讲师讲解"。取不到返回 None。

    三级回退，每一级都有明确的理由：
        ① `point_id` 精确匹配 —— 新数据走这条，最可靠；
        ② `point_id IS NULL` 且 tags 里带「知识点：标题」—— 老数据的补救；
        ③ 都没有 → 返回 None（调用方报"先让讲师讲一段"）。
    为什么不做第 ④ 级"拿最近一条别的知识点的"：那张图会和标题对不上，
    而用户完全看不出是哪里错了。
    """
    if point is None:
        return None

    base = Message.query.filter_by(session_id=session_id, agent="lecturer")
    exact = (
        base.filter(Message.point_id == point.id)
        .order_by(Message.id.desc())
        .first()
    )
    if exact is not None:
        return exact

    legacy_marker = f"知识点：{point_title}"
    for message in (
        base.filter(Message.point_id.is_(None))
        .order_by(Message.id.desc())
        .limit(50)
        .all()
    ):
        if legacy_marker in (message.tags or ""):
            return message
    return None


def make_diagram_worker(session_id: int, style_hint: str = "") -> Callable[[Callable[[dict], None]], str]:
    """图解是可选动作：点了按钮才跑，不进入主对话流程。"""

    def worker(publish: Callable[[dict], None]) -> str:
        """外壳：与教学回合**互斥**。

        图解也要写 `Message`、也要往同一个讲义文件里追加内容。如果它和
        教学回合并发，最坏的组合是"讲义被两份内容交错写入"——而 Markdown
        追加没有事务，错了也看不出来。所以它同样要拿会话锁。
        """
        with _runtime().session_lock(session_id):
            return _draw(publish)

    def _draw(publish: Callable[[dict], None]) -> str:
        from ..agents.visual import generate_diagram

        session = db.session.get(StudySession, session_id)
        if session is None:
            raise ValueError("学习会话不存在")

        ctx = current_context(session)
        chapter, point = ctx["chapter"], ctx["point"]
        point_title = point.title if point else ctx["course"].title

        # 用**当前知识点**的最近一次讲解作为素材。
        #
        # 原先的查询只按 session + agent 取最近一条，没有限定知识点：
        # 用户在左栏切到新知识点、还没来得及讲，就点"画成图解"时，
        # 会拿到**上一个知识点**的讲解，再用新知识点的标题去生成——
        # 图与标题对不上（评审 3.1 举的就是这个例子）。
        #
        # 回退链是刻意的：新数据按 `point_id` 精确匹配；老数据 `point_id` 为空，
        # 退回按 tags 里的「知识点：xxx」匹配；都匹配不到就报错让先讲一段，
        # **绝不**默默拿别人的内容。
        last = _lecturer_message_for_point(session.id, point, point_title)
        content = last.content if last else (point.summary if point else "")
        if not content:
            raise ValueError("还没有可用来画图的讲解内容，先让讲师讲一段")

        publish({"type": "status", "status": "画图中"})
        result = generate_diagram(point_title=point_title, content=content, style_hint=style_hint)

        db.session.add(Message(
            session_id=session.id,
            role="agent",
            agent="visual",
            content=result["code"],
            tags=json.dumps(["图解", f"知识点：{point_title}"], ensure_ascii=False),
            course_id=ctx["course"].id,
            point_id=point.id if point else None,
            turn_id=current_turn_id(),
        ))
        db.session.commit()

        # 图解同样归档到该知识点的讲义里，跟讲解放在一起
        _archive_to_lecture(
            publish,
            course=ctx["course"],
            chapter=chapter,
            point=point,
            content=result["code"],
            as_diagram=True,
        )

        # 把图代码单独推一个事件，前端直接交给 mermaid 渲染
        publish({"type": "diagram", "code": result["code"], "kind": result["kind"]})
        return result["code"]

    return worker


# ---------------- 进度推进 ----------------

def advance_point(session_id: int) -> dict:
    """把当前知识点标记为已讲，并推进到下一个。"""
    session = db.session.get(StudySession, session_id)
    if session is None:
        raise ValueError("学习会话不存在")

    ctx = current_context(session)
    course, point = ctx["course"], ctx["point"]
    if point:
        point.status = "done"

    chapter, nxt = curriculum_service.next_point(course, point.id) if point else (None, None)
    session.current_point_id = nxt.id if nxt else None
    session.current_chapter_id = (nxt.chapter.id if nxt else chapter.id if chapter else None)
    if nxt is None:
        session.status = "finished"
    db.session.commit()

    return {
        "current_point": {"id": nxt.id, "title": nxt.title} if nxt else None,
        "finished": nxt is None,
    }


def goto_point(session_id: int, point_id: int) -> dict:
    """把会话切到指定知识点（课程页左栏点击章节时调用）。

    与 advance_point 的区别：那个按大纲顺序推进，这个是任意跳转。
    """
    session = db.session.get(StudySession, session_id)
    if session is None:
        raise ValueError("学习会话不存在")

    point = db.session.get(KnowledgePoint, point_id)
    if point is None:
        raise ValueError("知识点不存在")
    if point.chapter.course_id != session.course_id:
        raise ValueError("这个知识点不属于当前课程")

    session.current_point_id = point.id
    session.current_chapter_id = point.chapter_id
    if session.status == "finished":  # 之前讲完了又跳回来，重新激活
        session.status = "active"
    db.session.commit()

    return {
        "current_chapter": {"id": point.chapter.id, "title": point.chapter.title},
        "current_point": {
            "id": point.id,
            "title": point.title,
            "summary": point.summary,
        },
    }


# ---------------- 视图数据 ----------------

def session_to_dict(session: StudySession) -> dict:
    ctx = current_context(session)
    chapter, point = ctx["chapter"], ctx["point"]
    return {
        "id": session.id,
        "status": session.status,
        "course": curriculum_service.course_to_dict(session.course),
        "current_chapter": {"id": chapter.id, "title": chapter.title} if chapter else None,
        "current_point": {
            "id": point.id,
            "title": point.title,
            "summary": point.summary,
            # 前端算"章末测试"链接要用它兜底（会话可能还没有 current_chapter）
            "chapter_id": point.chapter_id,
        } if point else None,
        # 教案进度（第几步 / 共几步）：课程页右侧那把"进度尺"**首屏**就要显示它。
        # 只靠授课时的进度事件不够——刷新页面之后就没有事件了，刻度会空着。
        # 与 `progress_snapshot` 里的那份同源（lesson_service.to_dict），不会各说各话。
        "plan": lesson_service.to_dict(lesson_service.get_plan(point.id)) if point else {},
        # 每个知识点的"讲到第几步"：对话页的"讲到过"列表靠它标注。
        # 只给当前点的话，往回翻时别的点就只有一个标题，看不出讲到哪儿。
        "points_progress": _points_progress(session),
        "messages": [m.to_dict() for m in session.messages],
    }


def _points_progress(session: StudySession) -> list[dict]:
    """这门课里**已经建过教案**的知识点，各自讲到第几步。

    只返回有教案的点：没备过课的点本来就还没开始，报"第 1/0 步"没有意义。
    """
    rows: list[dict] = []
    course = session.course
    if course is None:
        return rows
    for chapter in course.chapters:
        for item in chapter.points:
            plan = item.lesson_plan
            if plan is None:
                continue
            total = len(plan.steps_list())
            if not total:
                continue
            rows.append({
                "point_id": item.id,
                "title": item.title,
                "current_step": plan.current_step or 0,
                "total_steps": total,
                "finished": plan.is_finished(),
            })
    return rows


# 会话读取是对外接口的一部分，统一从本模块暴露（实现复用课程服务，避免两处复制）
get_session = curriculum_service.get_session
