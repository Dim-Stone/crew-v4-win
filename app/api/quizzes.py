"""小测接口：取题、交卷、跳过。

为什么"出题"不在这里，而在 `/api/sessions/<id>/quiz`：
    出题要跑模型，必须走 `start_turn()`（超时 / 取消 / 断线恢复都在那条链上）；
    而本文件这几个动作都是**纯数据库 + 本地判分**，同步返回就行，不需要任务。
"""

from __future__ import annotations

import io
from datetime import datetime

from flask import Blueprint, request, send_file

from ..extensions import db
from ..models import Chapter, Quiz, StudySession
from ..services import quiz_docx, quiz_service
from ._common import fail, ok

bp = Blueprint("quizzes", __name__, url_prefix="/api/quizzes")


def _load(quiz_id: int) -> Quiz | None:
    return db.session.get(Quiz, quiz_id)


@bp.get("/<int:quiz_id>")
def get_quiz(quiz_id: int):
    """取题面。**判分之前不返回答案**（见 `Quiz.to_dict` 的说明）。"""
    quiz = _load(quiz_id)
    if quiz is None:
        return fail("这份小测不存在", 404)
    return ok(quiz.to_dict())


@bp.post("/<int:quiz_id>/submit")
def submit_quiz(quiz_id: int):
    """交卷并判分。

    判分是**同步**的（没有模型调用），所以这里直接返回逐题结果，
    前端不必再订阅一条流。这也意味着"交卷"这个动作不会有超时问题。
    """
    quiz = _load(quiz_id)
    if quiz is None:
        return fail("这份小测不存在", 404)
    if quiz.status == "graded":
        # 已经判过的卷子再交一次：允许，但会覆盖（重做一遍是合理的）
        pass

    data = request.get_json(silent=True) or {}
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return fail("answers 必须是一个对象：{题号: 作答}")
    if not answers:
        return fail("还没有作答")

    return ok(quiz_service.grade_quiz(quiz, answers))


@bp.post("/<int:quiz_id>/skip")
def skip_quiz(quiz_id: int):
    """跳过这次小测。

    **跳过不等于通过**：不记分数、不进掌握度，只留一条"未测"的证据。
    这是刻意的——把跳过算成掌握，会让后面所有"根据掌握度决定讲什么"都失真。
    """
    quiz = _load(quiz_id)
    if quiz is None:
        return fail("这份小测不存在", 404)
    return ok(quiz_service.skip_quiz(quiz))


@bp.get("/<int:quiz_id>/export.docx")
def export_docx(quiz_id: int):
    """把这份测验导出成 Word，打印出来就能做。"""
    quiz = _load(quiz_id)
    if quiz is None:
        return fail("这份小测不存在", 404)

    session = db.session.get(StudySession, quiz.session_id)
    course_title = session.course.title if session and session.course else ""
    chapter = db.session.get(Chapter, quiz.chapter_id) if quiz.chapter_id else None
    chapter_title = chapter.title if chapter else ""

    try:
        data = quiz_docx.build_docx(
            quiz, course_title=course_title, chapter_title=chapter_title
        )
    except Exception:  # noqa: BLE001 —— 导出失败要说清楚，别给一个坏文件
        import logging

        logging.getLogger(__name__).exception("导出 Word 失败")
        return fail("导出 Word 失败，可以稍后再试", 500)

    name = f"{(chapter_title or '测验')}-{datetime.now():%Y%m%d}.docx"
    return send_file(
        io.BytesIO(data),
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        as_attachment=True,
        download_name=name,
    )
