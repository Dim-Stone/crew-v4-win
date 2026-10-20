"""讲义工作区（`notes/`）的只读浏览。

为什么要有这一页：讲义是每轮授课自动落盘的产物，而在这一页之前，用户只能在
课程页的进度尺里看到"**这次打开之后**存下来的"那几份——关掉再打开就没了，
可他手里其实已经攒了一摞文件。这一页把它们如实列出来，并且能在应用里直接读。

**只读，而且只能读工作区内的文件。** `/content` 的路径来自查询参数，所以要防
目录穿越——见 `_inside_root()`。那一条不是形式主义：这个服务虽然只监听
127.0.0.1，但项目根就躺着用户的 `.env`（明文密钥），一个
`?path=../../.env` 就是一次泄露。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Blueprint, request

from ..services.lecture_notes import lecture_root
from ._common import fail, ok

bp = Blueprint("notes", __name__, url_prefix="/api/notes")

#: 只认这个后缀。工作区里本来就只有 `.md`，但接口是公开的，收窄一点没坏处。
_ALLOWED_SUFFIX = ".md"


def _root() -> Path:
    """工作区根目录的**绝对且已解析**的形式。

    一律经过它：`lecture_root()` 可能是相对路径（配置写成相对时以项目根为基准），
    而"路径在不在工作区里"这件事只有在两边都 `resolve()` 之后比较才算数的。
    """
    return lecture_root().resolve()


def _display(path: Path) -> str:
    """给界面用的相对路径（一律 `/` 分隔，前端不必管平台差异）。"""
    return path.relative_to(_root()).as_posix()


def _size_text(size: int) -> str:
    """给人看的大小。KB 以下直接说字节——"0.4 KB"不如"410 字节"直观。"""
    if size < 1024:
        return f"{size} 字节"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _entry(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": _display(path),          # 相对工作区；读内容时原样传回来
        "name": path.name,
        "size": stat.st_size,
        "size_text": _size_text(stat.st_size),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _inside_root(rel: str) -> Path | None:
    """把参数里的相对路径解析成工作区里的真实文件；越界或不存在都返回 None。

    判据是**解析之后**的路径在不在工作区下面（`resolve()` 会把 `..` 和符号链接
    一起算掉），**不是**检查字符串里有没有 `..`：后者挡得住 `../`，挡不住
    符号链接、大小写混写，以及 `..` 被编码成各种形式的花样。
    """
    root = _root()
    try:
        target = (root / (rel or "")).resolve()
    except (OSError, RuntimeError):   # 符号链接成环之类的怪路径
        return None
    if not target.is_relative_to(root):
        return None
    if target.suffix.lower() != _ALLOWED_SUFFIX or not target.is_file():
        return None
    return target


@bp.get("")
def list_notes():
    """按课程分组列出工作区里的讲义。

    工作区不存在**不是错误**：全新装好、一份讲义都还没写过就是那个状态，
    界面据此说一句"还没有讲义"即可。
    """
    root = _root()
    if not root.is_dir():
        return ok({"root": str(root), "exists": False, "courses": [], "total": 0})

    courses = []
    for course_dir in sorted((p for p in root.iterdir() if p.is_dir()),
                             key=lambda p: p.name):
        files = sorted((f for f in course_dir.glob(f"*{_ALLOWED_SUFFIX}") if f.is_file()),
                       key=lambda f: f.name)
        if not files:
            continue
        courses.append({"course": course_dir.name, "files": [_entry(f) for f in files]})

    return ok({
        "root": str(root),
        "exists": True,
        "courses": courses,
        "total": sum(len(course["files"]) for course in courses),
    })


@bp.get("/content")
def read_note():
    """读一份讲义。路径是**相对工作区**的（就是 `/` 返回的 `path` 字段）。"""
    target = _inside_root(request.args.get("path", ""))
    if target is None:
        # 故意不区分"越界"与"不存在"：两者对用户是同一件事（这份打不开），
        # 而区分开来会让这个接口变成一台"哪个文件存在"的探测器。
        return fail("这份讲义不在工作区里（或者已经被删掉了）")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return fail(f"读不了这份讲义：{exc}")

    return ok({"path": _display(target), "name": target.name, "text": text})
