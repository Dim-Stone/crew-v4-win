"""① 摸底调查官。

职责：在正式开课前，摸清学习者的能力基础、兴趣、目的、时间与期望的授课风格，
并把回答解析成结构化画像。

设计取舍：**问题本身用预置问卷**（确定性、零 token、可复现），
**只有"把回答解析成画像"这一步走模型**（这是需要理解自然语言的部分）。
"""

from __future__ import annotations

import json
from typing import Any

from ..intake_questions import INTAKE_QUESTIONS
from .base import extract_json, load_prompt, run_json_once

_ALLOWED_LEVELS = {"beginner", "intermediate", "advanced"}


def _parse_profile(raw: str) -> dict[str, Any]:
    """解析画像，并收敛字段，避免模型自由发挥导致键名漂移。"""
    data = extract_json(raw)

    if not isinstance(data, dict):
        raise ValueError(f"画像解析结果不是对象：{type(data).__name__}")

    ability = str(data.get("ability_level", "unknown")).strip().lower()
    if ability not in _ALLOWED_LEVELS:
        ability = "unknown"

    return {
        "ability_level": ability,
        "interests": str(data.get("interests", "")).strip(),
        "purpose": str(data.get("purpose", "")).strip(),
        "style_preference": str(data.get("style_preference", "")).strip(),
        "time_budget": str(data.get("time_budget", "")).strip(),
        "summary_md": str(data.get("summary_md", "")).strip(),
    }


def parse_intake(direction: str, answers: dict[str, str]) -> dict[str, Any]:
    """把摸底回答解析成结构化画像。"""
    qa_lines = []
    for item in INTAKE_QUESTIONS:
        answer = (answers.get(item["key"]) or "").strip() or "（未回答）"
        qa_lines.append(f"- {item['label']}：{answer}")
    qa_text = "\n".join(qa_lines)

    description = load_prompt("interviewer").format(
        direction=direction, qa_text=qa_text
    )

    parsed = run_json_once(
        role="学习者画像分析师",
        goal="把摸底问答整理成准确、克制的学习者画像",
        backstory=(
            "你是一名有经验的教学设计师，擅长在两三句回答里看出一个学习者真正的"
            "起点、动机与偏好。你不会拔高也不会贬低，只记录能实际指导教学的信息。"
        ),
        description=description,
        expected_output=(
            "一个 JSON 对象，字段为 ability_level/interests/purpose/"
            "style_preference/time_budget/summary_md"
        ),
        parser=_parse_profile,
        temperature=0.2,  # 解析任务要稳，不要发散
    )

    parsed["raw_intake"] = json.dumps(answers, ensure_ascii=False)
    return parsed
