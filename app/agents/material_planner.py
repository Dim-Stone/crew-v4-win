"""⑨ 资料编排师：以学习者给的资料为骨架排章节。

和「学习规划师」的分工：
    规划师**不知道资料存在**，它按这门学科通常的学习路径自由组织；
    这里则相反——**资料的结构就是骨架**，它只负责把资料整理成
    「一次讲解一个知识点」的可讲粒度，并把每个知识点映射回它来自哪里。

为什么映射（sources）必须留着：
    ① 讲师那一轮要按它去取资料原文；
    ② 学习者能核对「讲的是不是我的资料」——这是资料模式下唯一可信的验证方式。
    没有它，「依据资料讲」就只是句口号。

粒度约束（一个知识点 = 一次讲解）与规划师共用同一套理由，
那段说明没有变，因为踩过的坑是同一个：把章节级主题当成一个知识点，
学习者会被困在里面讲十几轮。
"""

from __future__ import annotations

from typing import Any

from ..materials import MODE_REFERENCE, MODE_STRICT, normalize_mode
from .base import extract_json, load_prompt, run_json_once

# 两种模式对应两份提示词。为什么不合成一份带条件段落：
#   两者的**核心要求正好相反**——严格版要求"绝不补充"，参考版要求"该补就补但标明"。
#   写成一份会让两边互相削弱，模型很容易取中间态：既补了、又不标。
PROMPTS = {
    MODE_STRICT: "material_planner_strict",
    MODE_REFERENCE: "material_planner_reference",
}


def _as_sources(value: Any) -> list[dict[str, str]]:
    """把 sources 收敛成 `[{"file": ..., "locator": ...}]`。

    **必须容错**：模型有时候返回对象，有时候只返回一串文字
    （"矩阵引论.pdf 第 2 页"）。后者不能让整批解析失败——
    把整串当作 file、locator 留空，取原文时会退化成"按文件名取"，
    粗一点但仍然可用；而且文件名是"包含匹配"，那串文字里含文件名，照样能命中。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"file": text, "locator": ""})
        elif isinstance(item, dict):
            file = str(item.get("file") or item.get("name") or "").strip()
            locator = str(item.get("locator") or item.get("page") or "").strip()
            if file or locator:
                out.append({"file": file, "locator": locator})
    return out


def _parse_outline(raw: str) -> list[dict[str, Any]]:
    """解析大纲，并做字段收敛。

    结构与 `planner._parse_outline` 同构，差别只在于**多保留 `sources`**——
    资料模式下它是必需字段，不是可选装饰。
    """
    data = extract_json(raw)
    chapters = data.get("chapters") if isinstance(data, dict) else data

    if not isinstance(chapters, list) or not chapters:
        raise ValueError(
            "资料编排师没有返回有效的大纲结构。"
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
                "sources": _as_sources(point.get("sources")),
            })

        if points:
            normalized.append({
                "title": str(chapter["title"]).strip(),
                "summary": str(chapter.get("summary", "")).strip(),
                "points": points,
            })

    if not normalized:
        raise ValueError(
            "资料编排师返回的章节里没有有效的知识点。"
            f"\n模型原始输出前 600 字：\n{(raw or '')[:600]}"
        )
    return normalized


def generate_outline_from_materials(
    *, digest: str, profile_summary: str, focus: str = "", mode: str = MODE_REFERENCE
) -> list[dict[str, Any]]:
    """依据资料摘要排出大纲。

    `mode` 决定"依据强度"（见 `app/materials.py`）：
      strict    资料就是大纲，不得超出、不得补充；
      reference 资料是最重要的输入之一，可按学科需要补全（补充的 sources 留空）。

    返回 [{title, summary, points:[{title,summary,difficulty,est_minutes,sources}]}]。
    """
    resolved = normalize_mode(mode)
    description = load_prompt(PROMPTS[resolved]).format(
        digest=digest or "（没有读到任何资料）",
        profile=profile_summary or "（还没有画像信息，按通用大学生水平处理）",
        focus=focus or "（用户没有特别说明）",
    )

    if resolved == MODE_STRICT:
        backstory = (
            "你的工作是**整理**而不是**创作**：资料的组织方式就是课程的组织方式。"
            "你清楚地知道，学习者选这个模式是因为他只信自己那份资料，"
            "所以你补进去的任何一点对他来说都是不受信任的、而且他分不出是你加的。"
            "因此你宁可让课程在资料断掉的地方停住，也不替它接上去。"
        )
        expected = (
            "一个 JSON 对象，形如 {\"chapters\":[{\"title\":..,\"summary\":..,"
            "\"points\":[{\"title\":..,\"summary\":..,\"difficulty\":..,"
            "\"est_minutes\":..,\"sources\":[..]}]}]}；每个知识点都必须有 sources"
        )
    else:
        backstory = (
            "你既懂这门学科，也知道学习者手上的资料可能不全。"
            "你会尽量沿用资料自己的组织方式（他翻回原文找得到），"
            "该补的地方补上，但**一定把补的部分标明**——"
            "把补充内容谎称来自资料，比补充本身有害得多。"
        )
        expected = (
            "一个 JSON 对象，形如 {\"chapters\":[{\"title\":..,\"summary\":..,"
            "\"points\":[{\"title\":..,\"summary\":..,\"difficulty\":..,"
            "\"est_minutes\":..,\"sources\":[..]}]}]}；sources 留空表示该点由你补充"
        )

    return run_json_once(
        role="课程编排师（依据资料）",
        goal="把学习者给的资料整理成循序渐进、可以逐点讲授的章节结构",
        backstory=backstory,
        description=description,
        expected_output=expected,
        parser=_parse_outline,
        temperature=0.3,  # 比自由编排保守：这里不该发挥
    )
