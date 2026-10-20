"""页面路由。

五个页面：

    /            工作台：课程集合
    /new         新建课程：方向 → 摸底 → 画像 → 大纲，一页内完成
    /course/<id> 课程页：左栏章节时间轴 + 右栏对话
    /notes       讲义：工作区里所有已落盘的讲义（只读）
    /settings    设置：环境配置（怎么连模型）+ 界面 + 快捷键 + 关于

原来的 /intake、/outline、/teach 已合并进上面这些页面。

`/settings` 是**本机窗口版**补上的一页：以前的桌面版侧栏有九个目的地，
换成网页界面之后只剩三个业务页面，用户就再也没有地方填密钥了——
所以这一页不是"锦上添花"，是"不补就没法用"。
（其余几个目的地各自的落点见 `docs/界面设计说明.md` 开头的时效说明。）

`/notes` 同样是补回来的：讲义本来只出现在课程页的进度尺里，而那一条讲的是
"**这次打开之后**存的"，关掉再打开就看不到已经攒下的那些文件了。
"""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("pages", __name__)


@bp.get("/")
def dashboard():
    return render_template("dashboard.html", page="dashboard")


@bp.get("/new")
def new_course():
    return render_template("new.html", page="new")


@bp.get("/course/<int:course_id>")
def course(course_id: int):  # noqa: ARG001 —— 课程 id 由前端从地址里取用
    return render_template("course.html", page="course", course_id=course_id)


@bp.get("/course/<int:course_id>/class")
def classroom(course_id: int):
    """课堂：课程下的**第二级页面**，不是课程页里的一个视图状态。

    为什么不复用课程页：两者的"可用面积"需求正好相反——
        对话页要左栏（章节导航）+ 气泡流 + 输入框；
        课堂页要一整块屏幕只放当前这一页。
    第一版把课堂做进课程页，靠隐藏左栏腾地方，结果 `.workbench` 的
    `grid-template-columns: 296px 1fr` 仍然占着第一列，卡片被压到 296px 宽
    （用户截图里那个"一字一行"的标题）。分成两级页面之后，课堂页有自己的
    骨架，不再继承课程页的栅格。
    """
    return render_template("classroom.html", page="classroom", course_id=course_id)


@bp.get("/course/<int:course_id>/chapter/<int:chapter_id>/test")
def chapter_test(course_id: int, chapter_id: int):
    """章末测试：一章讲完之后的中型测试。

    单独一页而不是塞进课堂：它要能打印、能导出 Word，
    和"一页一卡地讲"是两种使用场景。
    """
    return render_template(
        "chapter_test.html", page="chapter_test",
        course_id=course_id, chapter_id=chapter_id,
    )


@bp.get("/notes")
def notes():
    return render_template("notes.html", page="notes")


@bp.get("/settings")
def settings():
    return render_template("settings.html", page="settings")

