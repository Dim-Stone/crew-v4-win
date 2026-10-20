"""讲义自动保存：把授课内容写成本地 Markdown。

为什么要有这个：
    讲义的价值在于"以后还能翻"。如果只停在对话里，一刷新就得重新找。
    所以每轮授课结束后自动落盘，用户不需要做任何导出操作。

目录约定（`LECTURE_DIR` 就是"工作区"，可在 .env 里改，也支持绝对路径）：

    notes/
    └─ 景观生态学/                        ← 按课程分子目录
       └─ 第01章-景观生态学基础.md         ← **一章一个文件**
          # 第1章 景观生态学基础          ← 章（文件标题）
          ## 1.1 景观的定义              ← 知识点（节）
          ### 第 1 轮 · 时间             ← 一次讲授
          ### 图解 · 时间

**为什么是"一章一个文件"，而不是"一个知识点一个文件"**：

    学生复习的单位是**一章**，不是某个孤立知识点。按知识点散成一堆文件后，
    既看不出"这一章讲了些什么、先后是什么"，也没地方安放章末的整体梳理。
    更要紧的是——文件名里的序号是**字符串**，`1.10-` 会排在 `1.2-` 前面，
    顺序一旦交给文件名排序就注定是错的。现在顺序由**数据库的 order_index** 决定：
    新节按序号插进正确的位置，跟用户实际讲课的顺序无关（跳着讲也不会乱）。

写文件失败绝不能影响授课本身，所以调用方会包一层容错。
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from flask import current_app

from ..config import BASE_DIR

# Windows 文件名非法字符 + 换行
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_UPDATED_RE = re.compile(r"^> 最近更新：.*$", re.M)
# 章内的知识点节：## 1.2 景观要素与结构
_SECTION_RE = re.compile(r"^## (\d+)\.(\d+)[ \t]+(.*)$", re.M)
# 节内的轮次：### 第 3 轮 · 2026-09-22 10:30
_ROUND_RE = re.compile(r"^### 第 (\d+) 轮", re.M)


def lecture_root() -> Path:
    """讲义工作区目录。配置为相对路径时，以项目根目录为基准。"""
    root = Path(current_app.config.get("LECTURE_DIR") or (BASE_DIR / "notes"))
    if not root.is_absolute():
        root = BASE_DIR / root
    return root


def _safe_name(text: str, max_len: int = 80) -> str:
    cleaned = _ILLEGAL.sub("-", str(text or "")).strip(" .-")
    return cleaned[:max_len] or "未命名"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _display(path: Path) -> str:
    """返回给用户看的路径：在项目内就用相对路径，否则用绝对路径。"""
    try:
        return str(path.relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        return str(path)


def course_dir(*, course_id: int, course_title: str) -> Path:
    """某门课的讲义目录：`{课程ID}-{标题}`。

    **为什么必须以 ID 打头**（评审 3.3）：目录原先只由课程标题生成，
    两门标题清理后相同的课程会共用同一个目录；删除其中一门时
    `remove_course_dir()` 会把另一门的讲义一起删掉（按目录删，不看归属）。
    加上 ID 之后目录天然唯一，标题仍排在前面，人打开文件夹照样看得懂。

    补零到 4 位是为了让资源管理器里按名称排序时，新建的课排在后面——
    和章号补零是同一个道理（顺序的权威仍在数据库，这只是让"随手看"也正确）。
    """
    return lecture_root() / f"{int(course_id):04d}-{_safe_name(course_title)}"


def chapter_path(*, course_id: int, course_title: str, chapter_title: str,
                 chapter_index: int = 0) -> Path:
    """某一章的讲义文件路径。

    `chapter_index` 来自 `curriculum_service.point_position()`，**已经是 1 起**
    （它的 docstring 里写了"都从 1 开始"；这里不要再加一次，否则章号会整体偏一位）。

    文件名补零（第01章 而不是 第1章）：这样用户在资源管理器里按名称排序也是对的。
    但**文件的正确顺序不依赖它**——章与节的顺序都由数据库序号决定，
    补零只是为了让"随手打开文件夹"看到的样子也正确。
    """
    directory = course_dir(course_id=course_id, course_title=course_title)
    stem = f"第{chapter_index:02d}章-" + (_safe_name(chapter_title, 60) if chapter_title else "未命名章节")
    return directory / f"{stem}.md"


def _chapter_header(*, course_title: str, chapter_title: str, chapter_index: int) -> str:
    title = f"第{chapter_index}章 {chapter_title}".strip()
    return "\n".join([
        f"# {title}",
        "",
        f"> 课程：{course_title}",
        f"> 最近更新：{_now()}",
        "",
    ])


def _section_heading(*, chapter_index: int, point_index: int, point_title: str) -> str:
    return f"## {chapter_index}.{point_index} {point_title}"


def _find_section(text: str, *, chapter_index: int, point_index: int) -> tuple[int, int] | None:
    """定位知识点所在节的 [起, 止) 位置；返回 None 表示这一节还没有。

    "止"是下一个同级节的开头，不是文件末尾——所以往节里追加内容时，
    不会把内容写到下一个知识点下面去。
    """
    matches = list(_SECTION_RE.finditer(text))
    for i, match in enumerate(matches):
        if (int(match.group(1)), int(match.group(2))) == (chapter_index, point_index):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return match.start(), end
    return None


def _insert_offset(text: str, *, chapter_index: int, point_index: int) -> int:
    """新节该插在哪里：第一个序号比它大的节之前；都比它小就放末尾。

    这就是"跳着讲也不会乱"的保障——顺序来自序号比较，而不是到达时间。
    """
    target = (chapter_index, point_index)
    for match in _SECTION_RE.finditer(text):
        if (int(match.group(1)), int(match.group(2))) > target:
            return match.start()
    return len(text)


def _append_into_section(
    text: str,
    *,
    chapter_index: int,
    point_index: int,
    point_title: str,
    block: str,
) -> str:
    """把一段内容追加进对应知识点的节，必要时按序号新建这一节。"""
    where = _find_section(text, chapter_index=chapter_index, point_index=point_index)
    if where is None:  # 这一节还不存在
        offset = _insert_offset(text, chapter_index=chapter_index, point_index=point_index)
        before = text[:offset].rstrip("\n")
        after = text[offset:].lstrip("\n")
        heading = _section_heading(
            chapter_index=chapter_index, point_index=point_index, point_title=point_title
        )
        parts = [before, "", heading, "", block.rstrip("\n")]
        if after:
            # after 本身以换行结尾，这里先 rstrip 再统一补一个，
            # 否则每插入一节就会在文件末尾多攒一个空行
            parts += ["", after.rstrip("\n")]
        return "\n".join(parts) + "\n"

    start, end = where
    head = text[:start]
    section = text[start:end].rstrip("\n")
    tail = text[end:]
    merged = f"{section}\n\n{block.rstrip()}\n"
    return f"{head}{merged}{tail}"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _touch_updated(text: str) -> str:
    """把头部的"最近更新"改成当前时间。"""
    if _UPDATED_RE.search(text):
        return _UPDATED_RE.sub(f"> 最近更新：{_now()}", text, count=1)
    return text


def _load_text(
    path: Path, *, course_title: str, chapter_title: str, chapter_index: int
) -> str:
    """读入整章文件；还没有这个文件就生成一个只有标题的空章。"""
    if path.exists():
        return _touch_updated(path.read_text(encoding="utf-8"))
    return _chapter_header(
        course_title=course_title, chapter_title=chapter_title, chapter_index=chapter_index
    )


def _save_block(
    *,
    course_id: int,
    course_title: str,
    chapter_title: str,
    point_title: str,
    chapter_index: int,
    point_index: int,
    block: str,
) -> str:
    """读入整章文件 → 往对应节里追加一段 → 写回。"""
    path = chapter_path(
        course_id=course_id,
        course_title=course_title,
        chapter_title=chapter_title,
        chapter_index=chapter_index,
    )
    text = _load_text(
        path,
        course_title=course_title,
        chapter_title=chapter_title,
        chapter_index=chapter_index,
    )
    text = _append_into_section(
        text,
        chapter_index=chapter_index,
        point_index=point_index,
        point_title=point_title,
        block=block,
    )
    return _display(_write(path, text))


# ---------------------------------------------------------------- 对外的两个入口

def save_lecture(*, course_id: int, course_title: str, chapter_title: str, point_title: str,
                 content: str, chapter_index: int = 0, point_index: int = 0) -> str:
    """把一轮讲解写进该知识点所在的节，返回可读路径。"""
    content = (content or "").strip()
    if not content:
        return ""

    path = chapter_path(
        course_id=course_id,
        course_title=course_title,
        chapter_title=chapter_title,
        chapter_index=chapter_index,
    )
    existing = _load_text(
        path,
        course_title=course_title,
        chapter_title=chapter_title,
        chapter_index=chapter_index,
    )
    where = _find_section(existing, chapter_index=chapter_index, point_index=point_index)
    # 轮次要在**这一节内部**数，不是整章——否则第二个知识点会从"第 4 轮"开始
    scope = existing[where[0]:where[1]] if where else ""
    rounds = [int(n) for n in _ROUND_RE.findall(scope)]
    round_no = (max(rounds) + 1) if rounds else 1

    return _save_block(
        course_id=course_id,
        course_title=course_title,
        chapter_title=chapter_title,
        point_title=point_title,
        chapter_index=chapter_index,
        point_index=point_index,
        block=f"### 第 {round_no} 轮 · {_now()}\n\n{content}\n",
    )


def append_diagram(*, course_id: int, course_title: str, chapter_title: str, point_title: str,
                   mermaid_code: str, chapter_index: int = 0,
                   point_index: int = 0) -> str:
    """把一张 Mermaid 图解追加到该知识点所在的节里。

    图解挂在知识点下面而不是整章末尾：它解释的是**这个**知识点，
    放到章末就与讲解脱节了。
    """
    code = (mermaid_code or "").strip()
    if not code:
        return ""

    return _save_block(
        course_id=course_id,
        course_title=course_title,
        chapter_title=chapter_title,
        point_title=point_title,
        chapter_index=chapter_index,
        point_index=point_index,
        block=f"### 图解 · {_now()}\n\n```mermaid\n{code}\n```\n",
    )


def remove_course_dir(*, course_id: int, course_title: str) -> dict:
    """删掉某门课程的讲义目录（连同里面的文件）。

    **这是不可逆操作，所以安全校验比功能本身更重要。** 两道闸门：
      ① 目标必须是 `lecture_root()` 的**子路径**——否则一个奇怪的课程名
         就能把删除引到工作区外面去；
      ② 目标**不能等于** `lecture_root()` 本身，否则一次删除会把整个讲义工作区抹掉。

    任一条不满足就**什么都不做**，并把原因回给调用方（由界面提示出来）。
    目录本来就不存在则视为已完成（`removed=False, error=""`）。

    目录名带课程 ID，所以**只会删这一门课的**——旧实现只按标题，
    两门同名课程会互相误删（评审 3.3）。

    返回 `{"removed": bool, "path": str, "error": str}`。
    """
    root = lecture_root()
    target = course_dir(course_id=course_id, course_title=course_title)

    try:
        root_resolved = root.resolve()
        target_resolved = target.resolve()
    except OSError as exc:
        return {"removed": False, "path": _display(target), "error": str(exc)}

    if target_resolved == root_resolved:
        return {
            "removed": False,
            "path": _display(target),
            "error": "拒绝删除：目标是讲义工作区本身",
        }
    if root_resolved not in target_resolved.parents:
        return {
            "removed": False,
            "path": _display(target),
            "error": "拒绝删除：目标不在讲义工作区内",
        }

    if not target.exists():
        return {"removed": False, "path": _display(target), "error": ""}

    try:
        shutil.rmtree(target)
    except OSError as exc:
        return {"removed": False, "path": _display(target), "error": str(exc)}
    return {"removed": True, "path": _display(target), "error": ""}
