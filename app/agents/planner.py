"""② 学习规划师（对应立项书"学习路径规划"）。

职责：结合学习方向 + 用户画像，拆解出一份分章节、带知识点与难度的学习大纲。
产出是结构化 JSON，落库成 Course → Chapter → KnowledgePoint。

结构解析放在 `_parse_outline` 里，配合 `run_json_once` 失败自动重试——
模型偶发不按格式输出是常态，不能让它直接毁掉一次大纲生成。
"""

from __future__ import annotations

from typing import Any

from .base import extract_json, load_prompt, run_json_once


def _parse_outline(raw: str) -> list[dict[str, Any]]:
    """把模型输出解析成规范的大纲结构，并做字段收敛。"""
    data = extract_json(raw)
    chapters = data.get("chapters") if isinstance(data, dict) else data

    if not isinstance(chapters, list) or not chapters:
        # 把原始输出带上，否则这类失败无从诊断
        raise ValueError(
            "规划师没有返回有效的大纲结构。"
            f"解析出的类型：{type(chapters).__name__}；"
            f"顶层键：{list(data)[:10] if isinstance(data, dict) else '（不是对象）'}。"
            f"\n模型原始输出前 600 字：\n{(raw or '')[:600]}"
        )

    normalized: list[dict[str, Any]] = []
    for chapter in chapters:
        if not isinstance(chapter, dict) or not chapter.get("title"):
            continue

        points = []
        for point in chapter.get("points") or []:
            if isinstance(point, str):  # 模型偶尔直接给字符串数组
                point = {"title": point}
            if not isinstance(point, dict) or not point.get("title"):
                continue

            difficulty = str(point.get("difficulty", "medium")).lower()
            if difficulty not in {"easy", "medium", "hard"}:
                difficulty = "medium"

            try:
                minutes = int(point.get("est_minutes") or 15)
            except (TypeError, ValueError):
                minutes = 15

            points.append({
                "title": str(point["title"]).strip(),
                "summary": str(point.get("summary", "")).strip(),
                "difficulty": difficulty,
                "est_minutes": max(5, min(minutes, 120)),
            })

        if points:
            normalized.append({
                "title": str(chapter["title"]).strip(),
                "summary": str(chapter.get("summary", "")).strip(),
                "points": points,
            })

    if not normalized:
        raise ValueError(
            "规划师返回的章节里没有有效的知识点。"
            f"\n模型原始输出前 600 字：\n{(raw or '')[:600]}"
        )

    return normalized


def generate_outline(direction: str, focus: str, profile_summary: str) -> list[dict[str, Any]]:
    """生成大纲，返回 [{title, summary, points:[{title,summary,difficulty,est_minutes}]}]。"""
    description = load_prompt("planner").format(
        direction=direction,
        focus=focus or "（用户没有特别指定重点）",
        profile=profile_summary or "（还没有画像信息，按通用大学生水平处理）",
    )

    return run_json_once(
        role="学习路径规划师",
        goal="把任意学科拆成循序渐进、可以逐点讲授的章节与知识点结构",
        backstory=(
            "你是大学里带过很多届学生的课程设计者。你知道哪些概念必须排在前面，"
            "哪些是可以先跳过后面再补的，也清楚初学者最容易在哪一步卡住。"
            "你的大纲不追求面面俱到，而是追求「能真的按顺序学下去」。"
        ),
        description=description,
        expected_output=(
            "一个 JSON 对象，形如 {\"chapters\":[{\"title\":..,\"summary\":..,"
            "\"points\":[{\"title\":..,\"summary\":..,\"difficulty\":..,\"est_minutes\":..}]}]}"
        ),
        parser=_parse_outline,
        temperature=0.4,
    )
