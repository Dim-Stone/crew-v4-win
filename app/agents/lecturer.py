"""③ 授课讲师（对应立项书"内容创作" + "互动交流"）。

一期把这两个职责合在同一个智能体里，理由：
它们发生在**同一条对话流**中，提示词高度重叠，分开会导致每轮多一次调用、
上下文一致性也更难保证。提示词内部用「讲授」与「答疑」两种模式区分。

关键变化：讲师现在**沿着教案走**，而不是每轮即兴。
每轮装配时会给它四样东西（见 teaching_service）：
    plan_brief   这一节的教案视图——现在第几步、这一步要什么、还剩什么
    covered      这个知识点**已经讲过**的要点（避免重复讲）
    syllabus     只给当前位置附近的大纲，而不是整份
    materials_note  这一轮该用的资料原文——**按需挑出来的那几块**，不是整批资料
前三样合起来是"备课 → 按计划讲 → 不重复"的闭环；第四样把讲师这一步的要点
与学习者自己的资料接上（挑哪几块由当前这一步的要点与学生刚问的话决定，
见 materials_service.select_blocks）。

这个智能体是**流式**的：它的输出经 SSE 逐字推给前端，这是"流畅长对话"的关键。
"""

from __future__ import annotations

from typing import Iterator

from .base import load_prompt, stream_once


def build_description(
    *,
    course_title: str,
    chapter_title: str,
    point_title: str,
    point_summary: str,
    plan_brief: str,
    covered: str,
    syllabus_text: str,
    profile_summary: str,
    history_text: str,
    user_message: str,
    materials_note: str = "",
) -> str:
    return load_prompt("lecturer").format(
        course_title=course_title,
        chapter_title=chapter_title or "（未指定）",
        point_title=point_title or "（未指定）",
        point_summary=point_summary or "",
        plan=plan_brief or "（这个知识点还没有教案，按你自己对它的理解组织讲授顺序）",
        covered=covered or "（这个知识点还没有讲过内容）",
        syllabus=syllabus_text or "（无）",
        profile=profile_summary or "（暂无画像，按通用大学生水平处理）",
        history=history_text or "（这是本课程的第一轮对话）",
        user_message=user_message,
        # 兜底文案与 `materials_service.NO_MATERIALS` 同义：那边由服务层给出
        # （它更清楚这门课到底有没有资料），这里兜住"没人传"的情况。
        # 两处都改时请一起改。
        materials_note=materials_note or "（这次没有指定资料，按你自己的知识讲。）",
    )


def teach_stream(
    *,
    course_title: str,
    chapter_title: str,
    point_title: str,
    point_summary: str,
    plan_brief: str,
    covered: str,
    syllabus_text: str,
    profile_summary: str,
    history_text: str,
    user_message: str,
    materials_note: str = "",
) -> Iterator[str]:
    """流式产出一次授课/答疑回复。"""
    description = build_description(
        course_title=course_title,
        chapter_title=chapter_title,
        point_title=point_title,
        point_summary=point_summary,
        plan_brief=plan_brief,
        covered=covered,
        syllabus_text=syllabus_text,
        profile_summary=profile_summary,
        history_text=history_text,
        user_message=user_message,
        materials_note=materials_note,
    )
    yield from stream_once(
        role="授课讲师",
        goal="像一位讲得清楚又有耐心的大学老师那样，把知识点一次讲透一个",
        backstory=(
            "你在一所大学教书多年，最擅长把学生卡住的地方拆成能听懂的小步。"
            "你上课前会先想清楚这一节分几步走，然后按计划往下推，"
            "讲到哪里、下一步是什么，你心里有数。"
            "你会根据学生的反馈随时调整讲法：基础薄弱就补前置、觉得无聊就给动机和应用、"
            "想深挖就往下推一层。你从不一次倒完所有内容，也从不把讲过的东西再讲一遍。"
            "学习者给了自己的资料时，你会先去资料里找这一步用得上的原话和例子，"
            "再用它们把这一步讲透——讲的是这一步，不是资料的第几页到第几页。"
        ),
        description=description,
        expected_output="面向学习者的中文回复正文（Markdown，可含小标题、表格与公式），不要输出任何解释性的元话语",
        temperature=0.7,
    )
