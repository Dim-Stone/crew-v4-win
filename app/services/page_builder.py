"""把一段讲授正文切成"页"——纯函数，不碰数据库、不调模型。

## 为什么是确定性分页，而不是让模型按页产出

讨论时定的是"**先能简单实现**"：讲师仍按原来的方式生成整段讲授，
超长再自动分页。这样有两个好处：

    ① 讲师提示词不用改，现有讲授质量不会因为"按页限制篇幅"而退化；
    ② 分页是纯函数，可以写死测试（改坏了立刻知道），不引入新的不确定性。

代价是"页边界"由长度决定，可能落在不够自然的地方。所以下面这些规则都是
为了让边界**尽量不突兀**：优先在标题处断、其次在空行处断，绝不在
代码块、公式块、表格内部断——那会把学生看到的内容破坏掉。

## 三条硬规则

1. **不切开原子块**：``` 围栏代码、$$ 公式块、连续的表格行，都是一个整体。
2. **标题属于它下面的内容**：一页不会以标题结尾（否则翻页后标题孤零零留在上一页）。
3. **单块超长也要能分**：一个超长段落会在句子边界处切开 —— 否则一页会无限长。
   此时宁可切在中间，也不把整段塞进一页（那等于没有分页）。
"""

from __future__ import annotations

#: 一页的软上限（字符）。超过就在下一个块边界处换页。
#:
#: 900 这个数不是拍的：一屏卡片（含页脚操作条）大约放得下 30 行正文，
#: 中文按每行约 40 字算是 1200 字左右；留出公式/列表的额外行高，取 900。
PAGE_MAX_CHARS = 900

#: 合并块时，若加上这一块会超限，但当前页还不到这个长度，就先忍一下。
#:
#: 为什么需要它：否则一个 200 字的小标题块 + 800 字正文会被拆成
#: "一页标题" + "一页正文"，翻页体验很糟。
PAGE_MIN_CHARS = 260

#: 句子边界（用于切超长段落）。
_SENTENCE_ENDS = "。！？!?；;…"


def _blocks(content: str) -> list[str]:
    """把正文切成"不可再分的块"（按空行与标题，且不破坏代码/公式块）。"""
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        nonlocal current
        text = "\n".join(current).strip("\n")
        if text.strip():
            blocks.append(text)
        current = []

    for line in lines:
        stripped = line.strip()

        if fence is None:
            # 开围栏代码
            if stripped.startswith("```"):
                flush()
                fence = "```"
                current.append(line)
                continue
            # 开多行公式块（单行 $$...$$ 不算块）
            if stripped.startswith("$$") and not _is_inline_math(stripped):
                flush()
                fence = "$$"
                current.append(line)
                continue
            # 标题单独成块：保证它不会和下面的内容被拆到两页
            if stripped.startswith("#"):
                flush()
                current.append(line)
                continue
            # 空行 = 块边界
            if not stripped:
                flush()
                continue
            current.append(line)
            continue

        # ---- 围栏内部：原样收集，直到闭合 ----
        current.append(line)
        if fence == "```" and stripped.startswith("```"):
            flush()
            fence = None
        elif fence == "$$" and stripped.endswith("$$") and stripped != "$$":
            flush()
            fence = None
        elif fence == "$$" and stripped == "$$":
            flush()
            fence = None

    flush()
    return blocks


def _is_inline_math(stripped: str) -> bool:
    """`$$x^2$$` 是单行公式，不该被当成"块的开头"。"""
    if not stripped.startswith("$$"):
        return False
    return stripped.count("$$") >= 2


def _is_page_break_heading(block: str) -> bool:
    """这一块是不是"该从新的一页开始"的标题。

    只有一二级标题（`#` / `##`）算。原因很实际：讲师的产出通常用
    `# 第一步` / `## 例子` 这样的层级分段，正好对应"每步一页"；
    而 `###` 及更深的多半是一页内部的子标题，拿它分页会把一页切碎。
    """
    first_line = block.lstrip().split("\n", 1)[0]
    if not first_line.startswith("#"):
        return False
    level = len(first_line) - len(first_line.lstrip("#"))
    return 1 <= level <= 2


def _is_atomic(block: str) -> bool:
    """这一块能不能再被切分。

    代码块与公式块**不可切**：切开就不再是那段代码/公式了。
    代价是它们可能让一页明显超长——但要在这里切，只会得到坏内容。
    """
    head = block.lstrip()
    return head.startswith("```") or head.startswith("$$")


def _has_body(block: str) -> bool:
    """这一块里有没有"正文"（不只是标题行）。"""
    return any(
        line.strip() and not line.lstrip().startswith("#")
        for line in block.split("\n")
    )


def _split_long_block(block: str, max_chars: int) -> list[str]:
    """把一个超长块按句子边界切开（最后手段）。

    极端情况：一整段没有句号的文字（比如一个长公式列表），此时按字符硬切——
    总比让一页无限长好，而且这种情况本身说明内容不适合放一页。
    """
    pieces: list[str] = []
    rest = block
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = -1
        for index in range(len(window) - 1, max(0, len(window) // 3), -1):
            if window[index] in _SENTENCE_ENDS:
                cut = index + 1
                break
        if cut <= 0:
            cut = max_chars
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        pieces.append(rest.strip())
    return [piece for piece in pieces if piece]


def _title_of(text: str) -> str:
    """取这一页的标题：第一个 Markdown 标题；没有就空字符串。"""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:80]
    return ""


def paginate(content: str, *, max_chars: int = PAGE_MAX_CHARS,
             min_chars: int = PAGE_MIN_CHARS) -> list[dict]:
    """把正文分成页。返回 `[{"title": str, "content": str}, ...]`。

    空内容返回 `[]`（调用方据此决定不建页，而不是建一页空白）。
    """
    content = (content or "").strip()
    if not content:
        return []

    # 先切块，再把**可切**的超长块拆成能放进一页的大小。
    # 代码块 / 公式块原样保留（见 `_is_atomic`）：宁可让那一页很长，
    # 也不要把一段代码切成两半。
    blocks: list[str] = []
    for block in _blocks(content):
        if len(block) <= max_chars or _is_atomic(block):
            blocks.append(block)
        else:
            blocks.extend(_split_long_block(block, max_chars))

    pages: list[dict] = []
    current: list[str] = []
    size = 0
    body = False

    def flush() -> None:
        nonlocal current, size, body
        text = "\n\n".join(current).strip()
        if text:
            pages.append({"title": _title_of(text), "content": text})
        current = []
        size = 0
        body = False

    for block in blocks:
        block_len = len(block)

        # ① 一二级标题 = 页边界。但只在**当前页已经有正文**时才断：
        #    否则 "# 极限的定义" 和紧跟其后的 "## 直观理解" 会各自占一页，
        #    出现"只有一行标题"的空卡。
        if current and _is_page_break_heading(block) and body:
            flush()

        # ② 长度超限 = 页边界（当前页已经有点内容时才断，避免碎页）
        if current and size + block_len + 2 > max_chars:
            if size >= min_chars or block_len > max_chars:
                flush()

        current.append(block)
        size += block_len + 2
        if _has_body(block):
            body = True
    flush()

    # 收尾：把"一句话的尾页"并回上一页。
    #
    # 为什么允许越界 20%：一页只放一句"以上就是这一节的要点"会让翻页变得可笑，
    # 而略超软上限（900 → 最多 1080）只是稍微多滚一点。两害相权取轻。
    #
    # **但以标题开头的尾页不合并**：那是语义上的页边界（"# 第二步 …"），
    # 合并会把两步又粘回一页，等于分页失效。
    if len(pages) >= 2:
        tail = pages[-1]
        prev = pages[-2]
        merged_len = len(prev["content"]) + len(tail["content"]) + 2
        if (
            len(tail["content"]) < min_chars
            and not _is_page_break_heading(tail["content"])
            and merged_len <= int(max_chars * 1.2)
        ):
            prev["content"] = prev["content"] + "\n\n" + tail["content"]
            if not prev["title"]:
                prev["title"] = tail["title"]
            pages.pop()

    return pages
