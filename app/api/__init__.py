"""API 蓝图注册。"""

from . import (
    courses,
    day_notes,
    intake,
    materials,
    meta,
    notes,
    preferences,
    quizzes,
    sessions,
    settings,
    todos,
    turns,
)
from ._common import fail, ok, start_turn

BLUEPRINTS = (
    meta.bp,
    settings.bp,
    preferences.bp,
    notes.bp,
    day_notes.bp,
    courses.bp,
    intake.bp,
    materials.bp,
    sessions.bp,
    quizzes.bp,
    todos.bp,
    turns.bp,
)

__all__ = ["BLUEPRINTS", "fail", "ok", "start_turn"]
