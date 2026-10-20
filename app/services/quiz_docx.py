"""把一次测验导出成 Word（.docx），目标：**打印出来就能直接做**。

## 为什么单独一个模块

排版细节（字体、字号、行距、缩进、"题目不许跨页"、答案单独起页、页码）
和业务逻辑没有关系。混在 `quiz_service` 里会让那个文件变成"什么都改它"的地方。

## 排版上的几条硬规定

1. **中文字体必须显式设**：`python-docx` 只设 `font.name` 只作用于西文，
   中文会退回文档默认字体（常见是等线，打印出来很难看）。要同时写
   `w:eastAsia`。正文用宋体、标题用黑体，这是中文试卷的惯例。
2. **行距 1.5、段后 6pt**：给填空题留手写空间，也让长题干不挤在一起。
3. **题目不许跨页**：题干与选项设 `keep_with_next` / `keep_together`，
   否则会出现"选项在下一页"这种最招人烦的情况。
4. **答案单独起页**：学生打印时可以先只打题目那几页。
5. **页脚有页码**：装订/排序时用得上（用 Word 的域，不是写死的数字）。
6. **公式降级成可读文本**：docx 里没有 KaTeX。`\\dfrac{a}{b}` → `(a)/(b)`、
   `\\times` → `×` 之类，宁可朴素，也不要让学生看到一屏反斜杠。
"""

from __future__ import annotations

import io
import re
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

#: 题型在卷面上的顺序与标题。顺序固定：选择 → 多选 → 判断 → 填空。
SECTION_TITLES = (
    ("single_choice", "一、单项选择题"),
    ("multi_choice", "二、多项选择题"),
    ("judge", "三、判断题"),
    ("fill", "四、填空题"),
)

#: LaTeX 里常见的符号 → 可读字符。宁可朴素，也不要露出反斜杠。
_SYMBOLS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\ne": "≠", r"\neq": "≠", r"\approx": "≈", r"\pm": "±",
    r"\infty": "∞", r"\to": "→", r"\rightarrow": "→", r"\Rightarrow": "⇒",
    r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\cup": "∪", r"\cap": "∩",
    r"\sum": "Σ", r"\int": "∫", r"\partial": "∂", r"\pi": "π",
    r"\alpha": "α", r"\beta": "β", r"\theta": "θ", r"\lambda": "λ",
    r"\varepsilon": "ε", r"\epsilon": "ε", r"\mu": "μ", r"\sigma": "σ",
    r"\lim": "lim", r"\sin": "sin", r"\cos": "cos", r"\tan": "tan",
    r"\log": "log", r"\ln": "ln", r"\max": "max", r"\min": "min",
    r"\sup": "sup", r"\inf": "inf", r"\det": "det", r"\prod": "∏",
    r"\left": "", r"\right": "", r"\displaystyle": "",
    r"\, ": "", r"\;": "", r"\!": "", r"\quad": " ", r"\qquad": "  ",
}


def _plain(text: str) -> str:
    """把 Markdown + LaTeX 压成"能用宋体排出来"的纯文本。"""
    value = str(text or "")
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = value.replace("$", "")

    # 分数与根号先处理（要在去花括号之前，否则结构就没了）
    for _ in range(2):      # 允许一层嵌套
        value = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", value)
        value = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", value)

    # 极限/求和/积分带下标：先整体换成 lim(...)，避免留一个孤立的下划线。
    # 注意这里必须用**单层**转义：`r"\\(lim…)"` 在原始字符串里表示"反斜杠 + s"，
    # 匹配不到 `\s`（第一次就写错了，渲染出来是 `lim_(x→0)`）。
    value = re.sub(
        r"\\(lim|sum|int|prod)\s*_\{\s*([^{}]*?)\s*\}",
        lambda m: f"{_SYMBOLS['\\' + m.group(1)]}({m.group(2)})",
        value,
    )

    for command, symbol in _SYMBOLS.items():
        value = value.replace(command, symbol)

    value = re.sub(r"\^\{([^{}]*)\}", r"^(\1)", value)
    value = re.sub(r"_\{([^{}]*)\}", r"_(\1)", value)
    value = re.sub(r"\\[a-zA-Z]+", "", value)   # 其余命令：去掉，只留内容
    value = value.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def _set_base_style(doc: Document) -> None:
    """正文：宋体 + Times New Roman，五号（10.5pt），行距 1.5。"""
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    paragraph = style.paragraph_format
    paragraph.line_spacing = 1.5
    paragraph.space_after = Pt(6)


def _east_asia(run, name: str) -> None:
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def _heading(doc: Document, text: str, *, size: int = 12) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(12)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    _east_asia(run, "黑体")


def _page_number_footer(doc: Document) -> None:
    """页脚居中显示"第 X 页"（用 Word 域，不只是个数字）。"""
    footer = doc.sections[0].footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("第 ")
    fld = run._element.makeelement(qn("w:fldSimple"), {qn("w:instr"): "PAGE"})
    run._element.addnext(fld)
    paragraph.add_run(" 页")


def build_docx(quiz, *, course_title: str = "", chapter_title: str = "") -> bytes:
    """生成这份测验的 .docx，返回字节。"""
    items = quiz.items()
    doc = Document()
    _set_base_style(doc)

    section = doc.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)
    _page_number_footer(doc)

    title = f"{_plain(chapter_title)}·章末测试" if chapter_title else "章末测试"
    heading = doc.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    heading.paragraph_format.space_after = Pt(2)
    run = heading.add_run(title)
    run.bold = True
    run.font.size = Pt(18)
    _east_asia(run, "黑体")

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.paragraph_format.space_after = Pt(14)
    meta_run = meta.add_run(
        f"{_plain(course_title)}　|　共 {len(items)} 题　|　"
        f"{datetime.now().strftime('%Y-%m-%d')}"
    )
    meta_run.font.size = Pt(9)
    meta_run.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    _east_asia(meta_run, "宋体")

    tip = doc.add_paragraph()
    tip.paragraph_format.space_after = Pt(10)
    tip_run = tip.add_run("姓名：__________　　得分：__________")
    tip_run.font.size = Pt(10)
    _east_asia(tip_run, "宋体")

    # 按题型分节；编号**跨题型连续**（1…n），和纸质卷一致
    counter = 0
    for kind, section_title in SECTION_TITLES:
        group = [item for item in items if item.get("type") == kind]
        if not group:
            continue
        _heading(doc, section_title)
        for item in group:
            counter += 1
            stem = doc.add_paragraph()
            stem.paragraph_format.space_before = Pt(6)
            stem.paragraph_format.space_after = Pt(2)
            stem.paragraph_format.keep_with_next = True
            stem.paragraph_format.keep_together = True
            stem.add_run(f"{counter}. ").bold = True
            stem.add_run(_plain(item.get("stem")))

            options = item.get("options") or []
            for index, option in enumerate(options):
                is_last = index == len(options) - 1
                line = doc.add_paragraph()
                line.paragraph_format.left_indent = Cm(0.75)
                line.paragraph_format.space_after = Pt(2)
                # 选项跟着题干走：最后一条也设 keep_with_next，
                # 让整题尽量落在同一页（Word 会把这个"链"当成一个整体）
                line.paragraph_format.keep_with_next = not is_last
                line.add_run(f"{option.get('key')}. {_plain(option.get('text'))}")

            if kind == "fill":
                answer_line = doc.add_paragraph()
                answer_line.paragraph_format.left_indent = Cm(0.75)
                answer_line.paragraph_format.space_after = Pt(6)
                answer_line.add_run("答：____________________________________")

    # ---- 答案与解析：单独起页 ----
    doc.add_page_break()
    _heading(doc, "参考答案与解析", size=14)

    for index, item in enumerate(items, start=1):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(6)
        paragraph.paragraph_format.keep_together = True
        answer = "、".join(_plain(one) for one in (item.get("answer") or []))
        paragraph.add_run(f"{index}. ").bold = True
        run = paragraph.add_run(f"答案：{answer}")
        run.bold = True
        if item.get("explain"):
            paragraph.add_run(f"　解析：{_plain(item.get('explain'))}")
        source = item.get("source")
        if isinstance(source, dict) and source.get("file"):
            note = f"　（出处：{_plain(source.get('file'))}"
            note += f" {_plain(source.get('locator'))}）" if source.get("locator") else "）"
            gray = paragraph.add_run(note)
            gray.font.size = Pt(9)
            gray.font.color.rgb = RGBColor(0x59, 0x59, 0x59)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
