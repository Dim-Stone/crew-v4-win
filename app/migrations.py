"""数据库升级：结构跟着 `create_all` 走，**数据回填**按版本号跑一次。

## 为什么不是 Alembic（先说清楚，免得下次又想换）

这个应用**以"打包成便携 zip / 安装版"交付**，用户机器上没有命令行、没有
`flask db upgrade` 这一步。所以迁移必须在**应用启动时自动完成**，
而不是靠运维执行一条命令。Alembic 的 `env.py` + 版本目录在这个交付形态下
要额外包一层"启动时调 alembic upgrade head"，并没有更简单。

真正缺的也不是"改表结构"的能力——`create_all()` 建新表、
`_ensure_missing_columns()` 给老表补**新增列**，这两步已经覆盖了
本项目至今所有的结构变更（都是"加列"）。缺的是**数据层面的迁移**：

    · 新加的列在老数据上是 NULL；
    · 语义变了的值（比如会话状态多了 `archived`）需要一次性整理。

所以这里只做一件事：**按版本号顺序执行数据迁移，且只执行一次**。
如果将来真需要"改列名 / 改类型 / 建索引"，再引 Alembic 也不迟——
那时把这些函数改写成 Alembic 的 upgrade() 是一一对应的。

## 为什么要版本号而不是"每次都跑一遍"

迁移里往往有全表扫描（见 `_m001` 的回填）。每次启动都跑一遍，
在课程与消息积累起来之后会明显拖慢启动。版本号保证它**只跑一次**。

`schema_meta` 表用裸 SQL 建：它不该成为一个业务模型，
也不该出现在 `db.metadata` 里（否则 `create_all` 会试图在每次启动重建它）。
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from sqlalchemy import text

from .extensions import db

log = logging.getLogger(__name__)

#: 当前代码期望的 schema 版本。新增迁移时**一起改这个数**。
SCHEMA_VERSION = 2


def _m001_backfill_message_associations() -> None:
    """给历史消息补上 `course_id` / `point_id`。

    v1 之前 `Message` 只有 `session_id` 与 `tags`，于是：
        · 图解要取"当前知识点的讲解"时只能猜；
        · 会话历史分组要按 `tags` 里的文本切开问答。

    回填规则（都只填**空值**，不覆盖已有数据）：
        · `course_id` ← 所属会话的 `course_id`；
        · `point_id`  ← `tags` 里「知识点：标题」在**同一门课**里匹配到的第一个知识点。
    匹配不到就留空——那正是"这条消息说不清属于哪个知识点"的诚实表达，
    前端对此有回退（见 `teaching_service._lecturer_message_for_point`）。
    """
    # 必须先 import 模型：迁移用 ORM，不 import 就没有映射
    from .models import KnowledgePoint, Message, StudySession

    session_courses = {
        session.id: session.course_id for session in StudySession.query.all()
    }
    filled_course = 0
    for message in Message.query.filter(Message.course_id.is_(None)).all():
        course_id = session_courses.get(message.session_id)
        if course_id is not None:
            message.course_id = course_id
            filled_course += 1
    db.session.commit()

    # 按 (course_id, 标题) 建索引，供 tags 回填时查找
    points_by_course: dict[int, dict[str, int]] = {}
    for point in KnowledgePoint.query.all():
        chapter = point.chapter
        if chapter is None:
            continue
        points_by_course.setdefault(chapter.course_id, {}).setdefault(point.title, point.id)

    filled_point = 0
    for message in Message.query.filter(Message.point_id.is_(None)).all():
        if message.course_id is None:
            continue
        try:
            tags = json.loads(message.tags or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str) or not tag.startswith("知识点："):
                continue
            title = tag[len("知识点："):]
            point_id = points_by_course.get(message.course_id, {}).get(title)
            if point_id is not None:
                message.point_id = point_id
                filled_point += 1
                break
    db.session.commit()

    log.info(
        "迁移 v1：回填消息关联（course_id %d 条、point_id %d 条）",
        filled_course, filled_point,
    )


def _m002_pages_from_messages() -> None:
    """把历史讲师消息回填成"页"，供卡片课堂显示。

    P0 的卡片是**从讲授正文切出来的**，所以在这次升级之前产生的讲授
    只有消息、没有页。不回填的话，学生打开旧课程会看到空白卡片区。

    回填规则（**宁可粗，也不猜**）：
        · 一条讲师消息按 `page_builder.paginate()` 切分，切成几页就建几页；
        · 页序在该 (会话, 知识点) 内从 0 递增；
        · `step_index` 留空（历史数据不知道当时讲到第几步，猜就是造假）；
        · 出处从 `tags` 里的「依据：…」还原；
        · 消息的 `page_id` 指向它的**第一页**；
        · **按 (会话, 知识点) 逐键判断要不要回填**，不是"库里有没有页"。

    最后一条是演练时抓出来的：最初写的是"只要库里已经有任何页就整段跳过"，
    本意是别覆盖运行期数据。但那太粗——在"一个库里有老会话 + 新会话"或者
    "又要合并一份旧库"的情况下，整段跳过会让**后来出现的老数据永远没有页**。
    逐键判断同样能保护运行期数据（已有的键不重复造），却不会漏掉新出现的旧数据。
    """
    from .models import LessonPage, Message
    from .services import page_builder

    messages = (
        Message.query
        .filter(Message.agent == "lecturer", Message.point_id.isnot(None))
        .order_by(Message.session_id, Message.id)
        .all()
    )

    # 已有的页：按 (session, point) 记下第一页 id 与下一个可用页序
    first_page_by_key: dict[tuple[int, int], int] = {}
    next_index: dict[tuple[int, int], int] = {}
    for page in LessonPage.query.order_by(LessonPage.index, LessonPage.id).all():
        key = (page.session_id, page.point_id)
        first_page_by_key.setdefault(key, page.id)
        next_index[key] = max(next_index.get(key, -1), page.index) + 1

    created = 0
    for message in messages:
        if (message.content or "").strip() == "":
            continue

        key = (message.session_id, message.point_id)
        if key in first_page_by_key:
            # 这个知识点已经有页了：不重复造，但把"消息 → 页"的链补上
            if message.page_id is None:
                message.page_id = first_page_by_key[key]
            continue

        chunks = page_builder.paginate(message.content)
        if not chunks:
            continue

        index = next_index.get(key, 0)

        materials = []
        try:
            tags = json.loads(message.tags or "[]")
        except (TypeError, ValueError):
            tags = []
        if isinstance(tags, list):
            materials = [
                tag[len("依据："):] for tag in tags
                if isinstance(tag, str) and tag.startswith("依据：")
            ]

        first_page = None
        for offset, chunk in enumerate(chunks):
            page = LessonPage(
                session_id=message.session_id,
                point_id=message.point_id,
                index=index + offset,
                kind="lecture",
                title=chunk["title"],
                content=chunk["content"],
                status="ready",
                turn_id=message.turn_id,
                materials=json.dumps(materials, ensure_ascii=False),
            )
            db.session.add(page)
            if first_page is None:
                first_page = page
            created += 1

        db.session.flush()
        if first_page is not None:
            message.page_id = first_page.id
        next_index[key] = index + len(chunks)

    db.session.commit()
    log.info("迁移 v2：从历史讲师消息回填 %d 页", created)


#: `{版本号: 迁移函数}`。键必须从 1 开始连续，且每个函数只做**数据**层面的事。
MIGRATIONS: dict[int, Callable[[], None]] = {
    1: _m001_backfill_message_associations,
    2: _m002_pages_from_messages,
}


def _ensure_meta_table() -> None:
    db.session.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL"
        ")"
    ))
    db.session.commit()


def get_version() -> int:
    row = db.session.execute(
        text("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    ).first()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def set_version(version: int) -> None:
    db.session.execute(
        text(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', :value) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        ),
        {"value": str(version)},
    )
    db.session.commit()


def run_migrations(app) -> None:
    """把还没跑过的迁移依次跑掉。**必须在 app context 里调用。**

    失败策略：**单条迁移失败不阻塞启动**。理由很实在——一个数据回填失败，
    比起"整门应用打不开"，显然是后者更糟；而且回填只影响可追溯性，
    功能本身有回退逻辑。失败时记 error 并在下一轮启动重试（版本号没推进）。
    """
    _ensure_meta_table()
    current = get_version()
    if current >= SCHEMA_VERSION:
        return

    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        # 描述取 docstring 的第一行，但**不能假定它存在**：
        # 一开始写成 `(fn.__doc__ or "").strip().splitlines()[0]`，
        # 遇到没有 docstring 的迁移函数（比如测试里换的 lambda）就 IndexError，
        # 而它在 try 里面——于是"迁移被静默跳过"，只在日志里留一条看不懂的错。
        description = (MIGRATIONS[version].__doc__ or "").strip().splitlines()
        try:
            log.info("运行数据迁移 v%d：%s", version,
                     description[0] if description else "（无说明）")
            MIGRATIONS[version]()
            set_version(version)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            app.logger.exception("数据迁移 v%d 失败（下次启动会重试）", version)
            return
