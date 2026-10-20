"""备课：为一个知识点写教案。

为什么单独一个角色，而不是让规划师顺手做掉：
    规划师关心的是**学科怎么拆**（章节顺序、前置依赖），粒度是"一门课"；
    备课关心的是**这一个点怎么讲**（分几步、每步要什么、学生在哪卡住），粒度是"一节课"。
    两者粒度不同，混在一个提示词里会互相打架。

生成时机是**按需**的：学习者第一次走进某个知识点时才备课。
这样既省调用，又能把"当时的学习者画像"带进教案——
而且教案里预见的卡点，是按这个人的实际情况写的，不是通用模板。
"""

from __future__ import annotations

from typing import Any

from .base import extract_json, load_prompt, run_json_once


def _parse_plan(raw: str) -> dict[str, Any]:
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError(f"教案的解析结果不是对象：{type(data).__name__}")

    steps: list[dict[str, Any]] = []
    for step in data.get("steps") or []:
        if isinstance(step, str):
            step = {"title": step}
        if not isinstance(step, dict) or not step.get("title"):
            continue

        key_points = step.get("key_points") or []
        if isinstance(key_points, str):
            key_points = [key_points]

        steps.append({
            "title": str(step["title"]).strip(),
            "intent": str(step.get("intent", "")).strip(),
            "key_points": [str(p).strip() for p in key_points if str(p).strip()],
            "example": str(step.get("example", "")).strip(),
        })

    if not steps:
        raise ValueError(
            "教案里没有有效的讲授步骤。"
            f"\n模型原始输出前 600 字：\n{(raw or '')[:600]}"
        )

    def as_list(key: str) -> list[str]:
        value = data.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item).strip()]

    return {
        "goal": str(data.get("goal", "")).strip(),
        "steps": steps,
        "pitfalls": as_list("pitfalls")[:6],
        "checks": as_list("checks")[:5],
    }


def draft_lesson_plan(
    *,
    course_title: str,
    chapter_title: str,
    point_title: str,
    point_summary: str,
    profile_summary: str,
) -> dict[str, Any]:
    """为一个知识点备课，返回 {goal, steps, pitfalls, checks}。"""
    description = load_prompt("lesson_planner").format(
        course_title=course_title,
        chapter_title=chapter_title or "（未指定）",
        point_title=point_title or "（未指定）",
        point_summary=point_summary or "",
        profile=profile_summary or "（暂无画像，按普通大学生处理）",
    )

    return run_json_once(
        role="备课教师",
        goal="把单个知识点拆成一条能顺畅讲下去的教案",
        backstory=(
            "你是一位带过多届学生的老师，习惯在开讲之前把这一节想清楚："
            "先讲什么、用什么例子、学生会在哪一步卡住、讲到什么程度算讲完。"
            "你从不把一节课堆成一团，而是分成几个明确的台阶，"
            "并且清楚上一个台阶没踩稳会怎样影响后面。"
        ),
        description=description,
        expected_output=(
            "一个 JSON 对象，字段为 goal / steps（每步含 title、intent、"
            "key_points、example）/ pitfalls / checks"
        ),
        parser=_parse_plan,
        temperature=0.4,
    )
