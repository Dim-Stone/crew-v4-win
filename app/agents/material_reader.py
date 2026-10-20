"""⑧ 材料阅读官：把一份资料读成结构化摘要。

为什么单独一个角色，而不并进规划师：
    规划师面对的是"整批资料的概览 + 学习者画像"，任务是**组织**；
    这里面对的是"一份材料本身"，任务是**如实读懂**。
    两者的约束正好相反——规划师可以按教学需要重新编排，
    而这里**必须照它原本的样子**：资料的结构一旦被改写，
    后面就再也对不回"这段内容出自哪一页"了，"依据资料"也就成了空话。

（编号接在现有角色之后。立项书里的四个智能体主线对应关系见 README，
这里新增的是资料模式下才用到的辅助角色。）
"""

from __future__ import annotations

from typing import Any

from .base import extract_json, load_prompt, run_json_once

# 喂给模型的单份材料上限。编摘要只需要知道"它在讲什么、怎么组织"，
# 不需要逐字读完——超长时宁可只看开头，也别把一次调用撑爆。
MATERIAL_LIMIT = 20000


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_digest(raw: str) -> dict[str, Any]:
    """解析并收敛字段，避免模型自由发挥导致键名漂移。"""
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError(f"材料摘要不是对象：{type(data).__name__}")

    topic = str(data.get("topic", "")).strip()
    if not topic:
        raise ValueError("材料摘要缺少 topic（这份材料在讲什么）")

    return {
        "topic": topic,
        "structure": _as_str_list(data.get("structure")),
        "key_points": _as_str_list(data.get("key_points")),
        "role": str(data.get("role", "")).strip(),
    }


def read_material(*, filename: str, content: str, total: int = 1) -> dict[str, Any]:
    """读一份材料，返回 {topic, structure, key_points, role, file}。"""
    body = (content or "").strip()
    if not body:
        raise ValueError(f"{filename} 没有可读的文字")

    description = load_prompt("material_reader").format(
        filename=filename,
        total=total,
        content=body[:MATERIAL_LIMIT],
    )

    parsed = run_json_once(
        role="教学材料阅读者",
        goal="如实读懂一份教学材料，报出它的主题、结构与重点",
        backstory=(
            "你读材料时只关心「这份材料说了什么」，而不是「这门学科是什么」。"
            "你会照抄材料自己的提法，宁可写「未提及」，也不拿自己的知识去补。"
        ),
        description=description,
        expected_output=(
            "一个 JSON 对象，键为 topic / structure / key_points / role"
        ),
        parser=_parse_digest,
        temperature=0.2,  # 如实汇报要稳，不要发散
    )

    parsed["file"] = filename
    return parsed
