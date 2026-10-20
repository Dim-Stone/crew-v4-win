"""④ 图解生成师（对应立项书"多模态生成"，一期轻量版）。

关键取舍：**不调用任何图像生成 API**，而是让模型产出 Mermaid 代码，
由前端本地渲染成流程图 / 思维导图。

好处：零额外成本、可人工修改、可导出、语法错误一眼可见。
这也正好把立项书"文本既是输出、也是创作脚本"的表述落到实处。
"""

from __future__ import annotations

import re

from .base import load_prompt, run_once

_FENCE = re.compile(r"```(?:mermaid)?\s*(.+?)\s*```", re.S)


def _clean_mermaid(text: str) -> str:
    """去掉围栏，只留 Mermaid 代码本体。"""
    text = (text or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    # 去掉可能被模型误加的围栏残留
    text = text.replace("```mermaid", "").replace("```", "").strip()
    return text


def _first_line_kind(code: str) -> str:
    for line in code.splitlines():
        line = line.strip()
        if not line or line.startswith("%%"):
            continue
        return line.split()[0].lower() if line.split() else ""
    return ""


def generate_diagram(*, point_title: str, content: str, style_hint: str) -> dict[str, str]:
    """生成一张 Mermaid 图。

    返回 {'code': 代码, 'kind': 图类型}，由前端交给 mermaid.js 渲染。
    """
    description = load_prompt("visual").format(
        point_title=point_title or "（未指定）",
        content=content[:3000],
        style_hint=style_hint or "flowchart",
    )

    raw = run_once(
        role="知识图解生成师",
        goal="把抽象知识转成结构清晰、一眼能看懂的 Mermaid 图",
        backstory=(
            "你擅长把一段文字里的层次、流程与因果关系抽出来，"
            "用最少的节点和文字表达清楚。你知道图的价值在于「看清结构」而不是「复述内容」。"
        ),
        description=description,
        expected_output="只包含一段 Mermaid 代码，不要有任何解释文字",
        temperature=0.3,
    )

    code = _clean_mermaid(raw)
    if not code:
        raise ValueError("图解生成师没有返回 Mermaid 代码")

    kind = _first_line_kind(code)
    if kind not in {"flowchart", "graph", "mindmap", "sequencediagram",
                    "classdiagram", "statediagram", "erdiagram", "timeline"}:
        # 不在白名单也照样返回，前端渲染失败时会给出提示，并让用户手动改
        kind = kind or "unknown"

    return {"code": code, "kind": kind}
