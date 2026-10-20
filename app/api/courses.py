"""课程与大纲接口。"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, request

from ..materials import uses_materials
from ..services import curriculum_service
from ._common import fail, ok, start_turn

bp = Blueprint("courses", __name__, url_prefix="/api/courses")


@bp.get("")
def list_courses():
    return ok([
        curriculum_service.course_overview(course)
        for course in curriculum_service.list_courses()
    ])


@bp.post("")
def create_course():
    data = request.get_json(silent=True) or {}
    direction = (data.get("direction") or "").strip()
    if not direction:
        return fail("请先填写你要学习的方向")

    mode = (data.get("material_mode") or "").strip().lower()
    path = (data.get("material_path") or "").strip()

    # 有资料却指了个不存在的目录：**当场说清楚**，别等后台跑起来才报。
    # 那时用户已经等了一会儿，而且不一定会回头去看进度里那行小字。
    if uses_materials(mode):
        if not path:
            return fail("选了资料模式，但还没有填资料文件夹")
        if not Path(path).is_dir():
            return fail(f"资料文件夹不存在或不是一个目录：{path}")

    course = curriculum_service.create_course(
        direction=direction,
        focus=(data.get("focus") or "").strip(),
        title=(data.get("title") or "").strip(),
        material_mode=mode,
        material_path=path,
    )
    return ok({"id": course.id, "title": course.title})


@bp.get("/<int:course_id>")
def get_course(course_id: int):
    course = curriculum_service.get_course(course_id)
    if course is None:
        return fail("课程不存在", 404)
    return ok(curriculum_service.course_to_dict(course))


@bp.delete("/<int:course_id>")
def delete_course(course_id: int):
    """删除课程——连带会话、回合，以及本地讲义目录。**不可恢复。**

    界面在调用前会弹确认框把后果说清楚；这里是最后一道闸，不是唯一的一道。
    正在生成大纲的课程会被拒绝（409）：后台线程还在往里写，
    删掉只会让那次规划以一个莫名其妙的错误收场。
    """
    try:
        summary = curriculum_service.delete_course(course_id)
    except ValueError as exc:
        return fail(str(exc), 409)  # 409：当前状态不允许这个操作
    if summary is None:
        return fail("课程不存在", 404)
    return ok(summary)


@bp.post("/<int:course_id>/outline")
def generate_outline(course_id: int):
    """生成大纲（异步）。返回 turn_id，前端用 SSE 订阅进度。"""
    course = curriculum_service.get_course(course_id)
    if course is None:
        return fail("课程不存在", 404)

    turn = start_turn(
        "outline",
        curriculum_service.make_outline_worker(course_id),
        course_id=course_id,
        payload={"course_id": course_id},
    )
    return ok({"turn_id": turn.id})
