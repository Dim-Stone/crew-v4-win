"""出题：把"这一节讲了什么"变成一组能判分的小测题。

## 为什么单独一个角色，而不是让讲师顺手出题

讲师的任务是**讲清楚**，它的提示词里已经装了教案、资料、画像、历史对话——
再塞"出题规则 + 参考答案 + 难度分布"，两件事会互相稀释（备课与规划当初分开
也是这个理由）。而且"讲的人自己出题"天然只考自己刚讲过的那一面，
查不出学生真正的薄弱处。

## 为什么不让它判分

判分是**规则**能做的事：选择题比集合、判断题比键、填空题归一化后比字符串。
交给模型只会引入不一致（同一份答案两次判分可能不同），而分数要用来做掌握度，
不一致的分数比没有分数更糟。所以这里只负责**出题**，判分在
`quiz_service.grade_item()` 里用纯逻辑做。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import extract_json, load_prompt, run_json_once

#: 允许的题型。与 `quiz_service` 的判分逻辑一一对应——加题型必须两处一起改。
TYPES = ("single_choice", "multi_choice", "fill", "judge")

_TYPE_LABEL = {
    "single_choice": "单选题",
    "multi_choice": "多选题",
    "fill": "填空题",
    "judge": "判断题",
}

_JUDGE_OPTIONS = [{"key": "A", "text": "正确"}, {"key": "B", "text": "错误"}]


def strip_option_label(key: str, text: str) -> str:
    """去掉选项文字里**自带**的编号。

    参数顺序是 **(key, text)**，与 `js/quiz_ui.js` 的 `stripOptionLabel` 一致——
    两边顺序不同是最容易埋下事故的那种不一致，所以刻意对齐。

    模型经常把选项写成 `{"key":"A","text":"A. 1"}`——前端又拼了一次 `A.`，
    卷面上就成了 `A. A. 1`（用户报过）。这里统一剥掉，让"编号"只有一个来源。

    两条规则：
        1. 先按**自己的 key** 剥：`A. 1` → `1`；
        2. 再按任意 A–D 剥一次：模型偶尔把编号写串（key=B、文字却以 `A.` 开头）。
    两条都**要求**编号后面有分隔符（`.、．:：,，)`）。只吃空格会把
    "A 大于 B" 这种正文也削掉——宁可少剥，不可错剥。
    """
    value = str(text or "").strip()
    if not value:
        return value

    separator = r"[.、．:：,，)）]"
    label = str(key or "").strip().upper()[:1]
    if label:
        own = rf"^[（(\s]*{re.escape(label)}[）)\s]*{separator}\s*"
        stripped = re.sub(own, "", value, count=1, flags=re.IGNORECASE).strip()
        if stripped and stripped != value:
            return stripped

    any_label = rf"^[（(\s]*[A-Da-d][）)\s]*{separator}\s*"
    stripped = re.sub(any_label, "", value, count=1).strip()
    # 剥完不能是空的（"A. A" 这种极端写法保留原样，总比空选项好）
    return stripped or value


def _normalize_options(raw: Any, *, kind: str) -> list[dict[str, str]]:
    """把模型给的选项收拾成 `[{"key","text"}]`，并保证键是 A/B/C/D。"""
    if kind == "judge":
        return list(_JUDGE_OPTIONS)

    options: list[dict[str, str]] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, str):
            key, text = chr(ord("A") + index), item
        elif isinstance(item, dict):
            key = str(item.get("key") or chr(ord("A") + index)).strip().upper()
            text = str(item.get("text") or "").strip()
        else:
            continue
        if text:
            final_key = key[:1] or chr(ord("A") + index)
            options.append({"key": final_key, "text": strip_option_label(final_key, text)})
    return options[:6]


def _parse_quiz(raw: str, *, count: int, types: tuple[str, ...]) -> dict[str, Any]:
    data = extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("出题结果不是含 items 的对象")

    items: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("items") or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()
        if kind not in types:
            # 模型偶尔会给出没被要求的题型：直接丢掉，而不是硬转
            continue

        stem = str(item.get("stem") or "").strip()
        if not stem:
            continue

        options = _normalize_options(item.get("options"), kind=kind)
        if kind in ("single_choice", "multi_choice") and len(options) < 2:
            continue
        if kind == "fill" and "____" not in stem and "＿" not in stem:
            # 填空题没有空位就没法答，补一个避免学生无从下手
            stem = f"{stem} ____"

        answers = item.get("answer")
        if isinstance(answers, (str, int, float)):
            answers = [answers]
        if not isinstance(answers, list):
            answers = []
        answers = [str(one).strip() for one in answers if str(one).strip()]
        if not answers:
            continue

        if kind == "judge":
            answers = [answers[0][:1].upper()] if answers[0][:1].upper() in ("A", "B") else []
            if not answers:
                continue
        if kind in ("single_choice", "multi_choice"):
            keyed = {option["key"] for option in options}
            answers = [key.upper()[:1] for key in answers]
            answers = [key for key in answers if key in keyed]
            if not answers:
                continue
            if kind == "single_choice":
                answers = answers[:1]
            else:
                answers = sorted(set(answers))
                if len(answers) < 2:
                    # 多选只有一个答案：降级成单选，别让它变成一道"其实是单选"的多选题
                    kind = "single_choice"

        step = item.get("step")
        try:
            step = int(step) if step is not None and str(step).strip() != "" else None
        except (TypeError, ValueError):
            step = None
        if step is not None and step < 0:
            step = None

        source = item.get("source")
        if not isinstance(source, dict) or not str(source.get("file") or "").strip():
            source = None
        else:
            source = {
                "file": str(source.get("file")).strip(),
                "locator": str(source.get("locator") or "").strip(),
            }

        items.append({
            "id": str(item.get("id") or f"q{index + 1}").strip()[:24] or f"q{index + 1}",
            "type": kind,
            "step": step,
            "stem": stem,
            "options": options,
            "answer": answers,
            "explain": str(item.get("explain") or "").strip()[:400],
            "difficulty": str(item.get("difficulty") or "medium").strip(),
            "source": source,
        })

    if not items:
        raise ValueError(
            "出题没有产生任何可判分的题目（可能题型不符或缺少答案）。"
            f"\n模型原始输出前 600 字：\n{(raw or '')[:600]}"
        )

    # 多给的不算错，但一次小测不该长到 12 题；少给的也接受（宁可少考，不要凑数）
    return {"items": items[: max(count + 2, 3)]}


def draft_chapter_quiz(
    *,
    course_title: str,
    chapter_title: str,
    chapter_summary: str,
    points: list[dict],
    count: int,
    types: tuple[str, ...],
    strict_materials: bool,
) -> dict[str, Any]:
    """章末测试：按**知识点**出题，覆盖整章。

    `points` 里每项形如 `{id, title, goal, steps, pitfalls}`。
    与本节小测的区别不只是题数：这里要求**每个知识点至少一题**、
    并且至少一道跨知识点的题——章末考的是"结构立没立起来"。
    """
    labels = "、".join(_TYPE_LABEL.get(kind, kind) for kind in types)
    blocks = []
    for index, item in enumerate(points, start=1):
        lines = [f"### 知识点 {index}（point_id={item['id']}）：{item['title']}"]
        if item.get("goal"):
            lines.append(f"目标：{item['goal']}")
        if item.get("steps"):
            lines.append(f"步骤要点：{item['steps']}")
        if item.get("pitfalls"):
            lines.append(f"常见错误：{item['pitfalls']}")
        blocks.append("\n".join(lines))

    template = load_prompt("quizmaster_chapter")
    description = (
        template
        .replace("[[course_title]]", course_title or "")
        .replace("[[chapter_title]]", chapter_title or "（未指定）")
        .replace("[[chapter_summary]]", chapter_summary or "")
        .replace("[[points]]", "\n\n".join(blocks) or "（这一章还没有可考的教案）")
        .replace("[[count]]", str(count))
        .replace("[[types]]", labels)
    )

    valid_points = {int(item["id"]) for item in points}

    def parse(raw: str) -> dict[str, Any]:
        data = extract_json(raw)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("章末测试的解析结果不是含 items 的对象")

        items: list[dict] = []
        for index, item in enumerate(data["items"]):
            if not isinstance(item, dict):
                continue
            try:
                point_id = int(item.get("point_id"))
            except (TypeError, ValueError):
                continue
            if point_id not in valid_points:
                # 标了不存在的知识点：这题没法归因，直接丢弃
                continue
            item = dict(item)
            item["point_id"] = point_id
            item["step"] = None
            item.setdefault("id", f"q{index + 1}")
            items.append(item)

        if not items:
            raise ValueError(
                "章末测试没有产生可归因到知识点的题目。"
                f"\n模型原始输出前 600 字：\n{(raw or '')[:600]}"
            )

        # 复用单节小测的清洗规则（题型/选项/答案/step），再把 point_id 带回来
        cleaned = _parse_quiz(
            json.dumps({"items": items}, ensure_ascii=False),
            count=max(count, len(items)),
            types=types,
        )
        by_id = {str(one.get("id")): one for one in items}
        for question in cleaned["items"]:
            source = by_id.get(str(question["id"]))
            question["point_id"] = source["point_id"] if source else None
        return cleaned

    return run_json_once(
        role="章末命题教师",
        goal="给学完整章的学生出一份覆盖各知识点重难点的中型测试",
        backstory=(
            "你编章末卷经验丰富：你知道章末不是把小测拼起来，"
            "而是要看学生能不能把这一章的概念连成一张网。"
            "你给每个知识点都留了位置，也一定会考一处容易混淆的对照。"
        ),
        description=description,
        expected_output=(
            "一个 JSON 对象，只有一个字段 items；"
            "每题含 id / type / point_id / stem / options / answer / explain / difficulty / source"
        ),
        parser=parse,
        temperature=0.5,
        retries=1,
    )


def draft_quiz(
    *,
    course_title: str,
    chapter_title: str,
    point_title: str,
    point_summary: str,
    steps_text: str,
    focus_text: str,
    materials_text: str,
    count: int,
    types: tuple[str, ...],
    strict_materials: bool,
) -> dict[str, Any]:
    """出一次小测，返回 `{"items": [...]}`。

    ## 为什么这里不用 `str.format()`

    提示词里有 JSON 示例（`{"key": "A", "text": ...}`），而 `format()` 会把
    花括号当占位符：轻则 `KeyError`，重则把 JSON 示例吃掉。
    出题面板第一版就是这么坏的（错误信息是 `'"key"'`，正是 `KeyError('key')` 的 repr）。
    换成 `[[name]]` 占位符 + 逐个替换：**提示词里可以随便写花括号**。
    """
    labels = "、".join(_TYPE_LABEL.get(kind, kind) for kind in types)
    if strict_materials:
        material_rule = (
            "学生要求严格依据资料。**不要出资料里没有的事实性题目**："
            "每道题的 `source` 必须填到具体文件与页，或者把 `source` 写 null 并且"
            "只在题干里明确标『综合』。宁可少出题，也不要编。"
        )
    else:
        material_rule = (
            "资料只是参考。可以用它出题，也可以根据这一节讲到的内容出题；"
            "凡是来自资料的，尽量把 `source` 填上；不来自资料的写 null。"
        )

    template = load_prompt("quizmaster")
    description = (
        template
        .replace("[[course_title]]", course_title or "")
        .replace("[[chapter_title]]", chapter_title or "（未指定）")
        .replace("[[point_title]]", point_title or "（未指定）")
        .replace("[[point_summary]]", point_summary or "")
        .replace("[[steps]]", steps_text or "（这一节没有明确的步骤记录，按知识点摘要出题）")
        .replace("[[focus]]", focus_text or "（没有单独记录重点难点，按步骤内容判断）")
        .replace("[[materials]]", materials_text or "（这一轮没有取到资料片段）")
        .replace("[[count]]", str(count))
        .replace("[[types]]", labels)
        .replace("[[material_rule]]", material_rule)
    )

    return run_json_once(
        role="出题教师",
        goal="给刚学完这一节的学生出一份能查出薄弱处的小测",
        backstory=(
            "你出题很有分寸：不考偏题怪题，也不靠记忆性的细节凑数。"
            "你只考那些『如果这里没真正理解，后面一定会出错』的地方，"
            "并且每道题都能说清它对应课堂上的哪一步。"
            "你出的填空题只填一个明确的数或式子，从不让人写作文。"
        ),
        description=description,
        expected_output=(
            "一个 JSON 对象，只有一个字段 items；"
            "每题含 id / type / step / stem / options / answer / explain / difficulty / source"
        ),
        parser=lambda raw: _parse_quiz(raw, count=count, types=types),
        temperature=0.5,
        # 出题慢主要是模型输出长。重试一次足够：再失败说明提示词该改，而不是再等一轮
        retries=1,
    )
