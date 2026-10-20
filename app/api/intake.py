"""摸底调查与画像接口。"""

from __future__ import annotations

from typing import Callable

from flask import Blueprint, request

from ..extensions import db
from ..intake_questions import INTAKE_QUESTIONS
from ..services import curriculum_service, profile_service
from ._common import fail, ok, start_turn

bp = Blueprint("intake", __name__, url_prefix="/api")


@bp.get("/intake/questions")
def questions():
    """预置摸底问卷：确定性、零 token。

    注意这里**不能**去 import app.agents —— 那会连带导入 crewai（首次 1.6s+），
    让一个纯数据接口白等一次重导入。问卷数据放在 app/intake_questions.py。
    """
    return ok([
        {"key": q["key"], "label": q["label"], "question": q["question"]}
        for q in INTAKE_QUESTIONS
    ])


@bp.post("/intake/answers")
def submit_answers():
    """提交摸底回答 → 异步解析成画像。"""
    data = request.get_json(silent=True) or {}
    answers = data.get("answers") or {}
    course_id = data.get("course_id")

    if not isinstance(answers, dict) or not answers:
        return fail("摸底回答是空的")

    course = curriculum_service.get_course(course_id) if course_id else None
    if course is None:
        return fail("课程不存在", 404)

    # 只把纯字符串带进线程池：ORM 对象不能跨线程使用
    course_id_value = course.id
    direction_value = course.direction
    payload = {"answers": answers, "course_id": course_id_value, "direction": direction_value}

    def worker(publish: Callable[[dict], None]) -> str:
        publish({"type": "status", "status": "正在整理你的画像"})
        merged = dict(answers)
        merged["_direction"] = direction_value
        profile = profile_service.update_from_intake(merged)
        info = profile_service.to_dict(profile)
        publish({"type": "profile", "data": info})
        return info.get("summary_md") or "画像已更新"

    turn = start_turn("intake", worker, payload=payload)
    return ok({"turn_id": turn.id})


@bp.get("/profile")
def get_profile():
    profile = profile_service.get_profile()
    return ok(profile_service.to_dict(profile))


@bp.put("/profile")
def update_profile():
    """允许手动修正画像——自动解析不一定准，人是最后决定者。"""
    data = request.get_json(silent=True) or {}
    profile = profile_service.get_profile()
    for field in ("ability_level", "interests", "purpose", "style_preference",
                  "time_budget", "summary_md"):
        if field in data:
            setattr(profile, field, str(data[field]))
    db.session.commit()
    return ok(profile_service.to_dict(profile))
