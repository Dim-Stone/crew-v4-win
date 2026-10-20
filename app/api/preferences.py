"""界面偏好接口：设置页里"界面"那一块。

为什么单独一个蓝图而不是并进 `/api/settings`：
    两件事的失败后果不一样。`/api/settings/llm` 管的是**怎么连模型**——
    改错了功能就不可用；这里是**怎么看界面**——改错了只是难看。
    分成两个蓝图之后，"保存密钥失败"与"保存字号失败"在日志与前端代码里
    一眼能分开，而它们本来就该分开处理（前者必须弹错误，后者可以放过）。

**没有"我的画像"接口**：那一块直接读现有的 `GET /api/profile`
（它本来就有，是新建课程的"画像"步骤在用）。这里再包一层只会多一个
可能与它行为不一致的入口。
"""

from __future__ import annotations

from flask import Blueprint, request

from .. import preferences as prefs_mod
from ._common import ok

bp = Blueprint("preferences", __name__, url_prefix="/api/preferences")


@bp.get("")
def read_preferences():
    """当前偏好 + 每个档位的说明（设置页靠它渲染选项）。"""
    return ok(prefs_mod.public_view())


@bp.put("")
def save_preferences():
    """保存若干项。

    **部分更新**：请求体里带哪几项就改哪几项，其余保持原值。
    这一点是必需的，因为设置页的表单会整体提交——将来加了新项之后，
    老版本界面提交的表单里不会有那个键，它不该被清掉。

    非法的值**不会**让整次保存失败：`save()` 会跳过那一项并记日志，
    其余有效的项照常生效。理由同上——"字号填错了"不该把"行宽"也一起退回去。

    **回读一次**再返回，而不是把刚提交的那份拼回去：响应里的值必须是
    **真正生效的**值。某项被跳过时，前端拿到回读结果重填表单，用户就能
    看到"我选的那个没被接受"，而不是以为保存成功了。
    """
    prefs_mod.save(request.get_json(silent=True) or {})
    return ok(prefs_mod.public_view())


@bp.post("/reset")
def reset_preferences():
    """恢复默认（删掉文件，回到"从来没设置过"的状态）。"""
    prefs_mod.reset()
    return ok(prefs_mod.public_view())
