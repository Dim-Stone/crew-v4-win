"""备课与讲授进度：把"计划"和"讲到哪了"留在上下文之外。

对应 Anthropic《Effective context engineering for AI agents》里的
**structured note-taking**（结构化笔记）：
    计划、进度、已讲要点存在数据库里，每轮只把当下需要的那一份拉回上下文。
    上下文里因此始终是"最小的高信号集合"，而不是把整段历史反复塞回去。

三件事：
    ensure_plan  按需备课（第一次走进某个知识点时）
    plan_brief   装配用的"教案视图"（现在是第几步、这一步要什么、还剩什么）
    review_turn  回合后复盘（把这一轮压成一句"已讲要点"，并更新进度）
"""

from __future__ import annotations

import json

from ..extensions import db
from ..models import KnowledgePoint, LessonPlan

# 同一个讲授步骤最多讲几轮，超过就**强制**往前走一步。
#
# 为什么需要这个硬性规则：复盘靠模型判断"讲透了没有"，而模型天然偏保守——
# 实测出现过 16 轮都停在同一个知识点、同一段内容重复六七遍的情况。
# 进度不能完全交给模型的心情，得有一条不依赖它的底线。
MAX_ROUNDS_PER_STEP = 3


def get_plan(point_id: int) -> LessonPlan | None:
    return LessonPlan.query.filter_by(point_id=point_id).first()


def ensure_plan(
    point: KnowledgePoint,
    *,
    profile_summary: str,
    course_title: str,
    chapter_title: str,
) -> LessonPlan:
    """没有教案就现备一份。

    备课是**按需**的：只在第一次走进这个知识点时发生，
    既省调用，也能把当时的学习者画像带进教案——
    所以预见的卡点是按这个人的实际情况写的，不是通用模板。
    """
    existing = get_plan(point.id)
    if existing is not None:
        return existing

    from ..agents.lesson_planner import draft_lesson_plan

    data = draft_lesson_plan(
        course_title=course_title,
        chapter_title=chapter_title,
        point_title=point.title,
        point_summary=point.summary,
        profile_summary=profile_summary,
    )

    plan = LessonPlan(
        point_id=point.id,
        goal=data["goal"],
        steps=json.dumps(data["steps"], ensure_ascii=False),
        pitfalls=json.dumps(data["pitfalls"], ensure_ascii=False),
        checks=json.dumps(data["checks"], ensure_ascii=False),
        current_step=0,
        covered="",
    )
    db.session.add(plan)
    db.session.commit()
    return plan


def plan_brief(plan: LessonPlan) -> str:
    """装配进提示词的「教案视图」。

    只给当下需要的那部分：当前这一步的要点与例子、已经走过哪些、还剩哪些。
    「已经讲过什么」由 covered 单独承担（见 teaching_service 的装配），
    两者分工：这里说"要讲什么"，covered 说"别重复什么"。
    """
    steps = plan.steps_list()
    if not steps:
        return ""

    total = len(steps)
    index = max(0, min(plan.current_step, total - 1))
    current = steps[index]

    lines: list[str] = []
    if plan.goal:
        lines.append(f"这一节的目标：{plan.goal}")
        lines.append("")

    lines.append(f"讲授进度：第 {index + 1} / {total} 步")

    done = [f"{i + 1}. {s['title']}" for i, s in enumerate(steps) if i < index]
    rest = [f"{i + 1}. {s['title']}" for i, s in enumerate(steps) if i > index]
    if done:
        lines.append("已经走过的步骤：" + "；".join(done))
    lines.append(f"**现在要讲的是第 {index + 1} 步：{current['title']}**")
    if current.get("intent"):
        lines.append(f"这一步要达到：{current['intent']}")
    for key_point in current.get("key_points") or []:
        lines.append(f"  - 必须讲到的要点：{key_point}")
    if current.get("example"):
        lines.append(f"  这一步准备的例子：{current['example']}")
    if rest:
        lines.append("后面还没讲：" + "；".join(rest))

    try:
        pitfalls = json.loads(plan.pitfalls or "[]")
    except json.JSONDecodeError:
        pitfalls = []
    if pitfalls:
        lines.append("")
        lines.append("学生在这一节最容易栽的地方（讲的时候要有意识地防）：")
        for item in pitfalls[:4]:
            lines.append(f"- {item}")

    return "\n".join(lines)


def review_turn(*, plan: LessonPlan, point_title: str, user_message: str, reply: str) -> None:
    """回合后复盘：这一轮讲到了哪、新讲了什么。

    这是 compaction 的落点——同一个知识点讲多轮时，历史不会无限堆积，
    而是被压成一段"已讲要点"，下一轮直接读它，不必重看整段对话。
    """
    steps = plan.steps_list()
    if not steps or not (reply or "").strip():
        return

    from ..agents.base import extract_json, load_prompt, run_once

    step_titles = "\n".join(
        f"{i + 1}. {s['title']}｜{s.get('intent', '')}" for i, s in enumerate(steps)
    )
    description = load_prompt("lesson_review").format(
        point_title=point_title,
        step_titles=step_titles,
        current_step=plan.current_step + 1,
        total_steps=len(steps),
        rounds_on_step=(plan.step_rounds or 0) + 1,
        covered=plan.covered or "（还没有记录）",
        user_message=user_message,
        reply=reply[:1500],
    )

    raw = run_once(
        role="授课复盘员",
        goal="判断这一轮推进到了第几步，并把新讲到的内容压成一句话",
        backstory=(
            "你负责在每轮课后做一分钟复盘：这一轮实际走到哪一步、"
            "新讲了什么、有没有只是在重复上一轮。你判断保守，不虚报进度。"
        ),
        description=description,
        expected_output="一个 JSON 对象，字段为 step_after / covered_add",
        temperature=0.2,
    )

    data = extract_json(raw)
    if not isinstance(data, dict):
        return

    # step_after 用 1 起编号（对人友好），这里换回 0 起
    try:
        step_after = int(data.get("step_after"))
    except (TypeError, ValueError):
        step_after = plan.current_step + 1
    new_index = max(0, min(step_after - 1, len(steps)))

    if new_index > plan.current_step:
        plan.current_step = new_index  # 正常前进
        plan.step_rounds = 0
    else:
        # 原地打转：记一轮；到上限就**强制**往前走一步。
        # 这是不依赖模型判断的兜底——模型的保守判断会把进度彻底锁死。
        plan.step_rounds = (plan.step_rounds or 0) + 1
        if plan.step_rounds >= MAX_ROUNDS_PER_STEP and not plan.is_finished():
            plan.current_step += 1
            plan.step_rounds = 0

    addition = str(data.get("covered_add", "")).strip()
    if addition:
        line = "- " + addition.lstrip("- ").strip()
        plan.covered = f"{plan.covered}\n{line}".strip() if plan.covered else line

    db.session.commit()


def review_point(
    *,
    plan: LessonPlan,
    course_title: str,
    chapter_title: str,
    point_title: str,
    pages_text: str,
) -> dict | None:
    """**整节**复盘：这一节讲成了什么样、哪里还虚。

    为什么从"每轮一次"改成"讲完这一节一次"（用户提的改进）：
        · 每轮复盘要一次模型调用，而它的问题（"这一轮讲到第几步"）在一节之内
          本来就很模糊，模型经常给不出稳定答案；
        · 讲完整节再看整体，才回答得了真正有用的问题：**哪一步讲虚了**。
          这个结论直接喂给章末/本节小测（`pitfalls`）和下一个知识点的备课。

    幂等：`reviewed_at` 已经写过就直接返回 None，不再花钱。
    失败不抛——复盘是附加价值，不能把已经上完的课弄崩。
    """
    if plan.reviewed_at is not None:
        return None

    from datetime import datetime, timezone

    from ..agents.base import extract_json, load_prompt, run_once

    steps = plan.steps_list()
    step_lines = []
    for index, step in enumerate(steps):
        mark = "（已讲）" if index < plan.current_step else "（未讲到）"
        step_lines.append(f"{index + 1}. {step.get('title', '')}{mark}")

    description = load_prompt("point_review").format(
        course_title=course_title,
        chapter_title=chapter_title or "（未指定）",
        point_title=point_title,
        goal=plan.goal or "（未填写）",
        steps="\n".join(step_lines) or "（没有记录步骤）",
        pages=pages_text[:6000] or "（没有取到讲授内容）",
    )

    raw = run_once(
        role="整节复盘员",
        goal="在知识点讲完之后，说清这一节讲成了什么样、哪里还虚",
        backstory=(
            "你只依据实际讲过的内容说话，不替学生宣布掌握，也不替教案补上没讲的部分。"
            "你尤其擅长指出'结论给了但推导跳了''例子只有一个'这类虚处。"
        ),
        description=description,
        expected_output="一个 JSON 对象，字段为 summary / gaps / off_topic",
        temperature=0.2,
    )

    data = extract_json(raw)
    if not isinstance(data, dict):
        return None

    summary = str(data.get("summary") or "").strip()[:400]
    gaps = data.get("gaps")
    gaps = [str(one).strip() for one in gaps] if isinstance(gaps, list) else []
    gaps = [one for one in gaps if one][:3]

    lines = []
    if summary:
        lines.append(f"- 整节复盘：{summary}")
    for gap in gaps:
        lines.append(f"- 复盘发现的虚处：{gap}")
    if lines:
        plan.covered = "\n".join([plan.covered] + lines).strip() if plan.covered else "\n".join(lines)

    # 虚处同时并进 pitfalls：小测提示词读的就是它，下次出题会照这些地方加题
    if gaps:
        existing = json.loads(plan.pitfalls or "[]")
        if isinstance(existing, list):
            merged = [str(one) for one in existing]
            for gap in gaps:
                if gap not in merged:
                    merged.append(gap)
            plan.pitfalls = json.dumps(merged[:8], ensure_ascii=False)

    plan.reviewed_at = datetime.now(timezone.utc)
    db.session.commit()
    return {"summary": summary, "gaps": gaps, "off_topic": bool(data.get("off_topic"))}


def to_dict(plan: LessonPlan | None) -> dict:
    if plan is None:
        return {}
    steps = plan.steps_list()
    index = plan.current_step
    return {
        "goal": plan.goal,
        "total_steps": len(steps),
        "current_step": index,
        "step_title": steps[index]["title"] if steps and index < len(steps) else "",
        "finished": plan.is_finished(),
    }
