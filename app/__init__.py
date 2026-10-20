"""应用工厂。

启动顺序：
    config → db → 任务运行时（线程池）→ 蓝图 → 建表
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from .config import Config
from .extensions import db
from .services.runtime import TurnRuntime


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )
    app.config.from_object(config_object)
    # 中文不要被转义成 \uXXXX，日志和接口都直读
    app.json.ensure_ascii = False

    # 目录必须先于数据库存在：SQLite 不会替我们建目录，`instance/` 缺了就直接连不上。
    # 便携包解压出来不一定带空目录（zip 默认不存空目录），所以放在**每次**启动都做。
    from .startup import ensure_dirs

    app.extensions["startup"] = {"dirs_created": ensure_dirs()}

    db.init_app(app)

    # 任务运行时：所有耗时的大模型调用都交给它，请求线程不被占用
    app.extensions["turn_runtime"] = TurnRuntime(app.config["TURN_MAX_WORKERS"])

    from . import pages
    from .api import BLUEPRINTS

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    app.register_blueprint(pages.bp)

    with app.app_context():
        # 一期简化：直接建表。
        # 结构变更至今全是"加新表 / 加新列"，`create_all` + `_ensure_missing_columns`
        # 已经覆盖；真正缺的是**数据**层面的迁移，见下面 `run_migrations`。
        app.logger.debug("已加载 %d 张表的定义", _load_models())
        db.create_all()
        _ensure_missing_columns(app)
        _fail_orphan_turns(app)

        # 数据迁移：按版本号把老数据里新增的关联列回填一次，只跑一次。
        # 失败**不阻塞启动**——一个回填失败不该让整门应用打不开（详见 migrations.py）。
        from .migrations import run_migrations

        run_migrations(app)

    _register_error_handlers(app)
    _disable_api_caching(app)
    return app


def create_local_app(config_object: type[Config] | None = None) -> Flask:
    """本机窗口版（`main.py` 的 pywebview 启动器）工厂。

    **它和 `create_app` 是同一个应用**——同一套蓝图、同一套模板与静态资源、
    同一个 `services/`。本机窗口版不是另一套实现，只是"谁来显示这些页面"不同：

        run_web.py      用你自己的浏览器打开
        main.py         用一个 pywebview 原生窗口打开（内核是系统的 Edge WebView2）

    那为什么还要单独一个工厂，而不是直接 `create_app(DesktopConfig)`：
        区别只有一处——**数据库与讲义默认落在项目目录内**（靠 `DesktopConfig`
        覆盖两个类属性做到），以及要不要把 `DESKTOP` 这个标记放进 `app.config`。
        建表、补列、清理被中断的回合这几步**必须走同一段代码**，
        所以这里直接调用 `create_app`，而不是把那 20 行抄一遍：
        抄一遍的下场是"网页版补了列、窗口版忘了补"，而那类毛病只在老库上出现。
    """
    if config_object is None:
        from .config_desktop import DesktopConfig

        config_object = DesktopConfig

    app = create_app(config_object)
    # 标记一下"这次是本机窗口版"。目前没有代码依赖它，留着是为了将来
    # （比如"窗口版不需要 CSRF、不需要登录"这类判断）有个明确落点。
    app.config["DESKTOP"] = True
    return app


def _load_models() -> int:
    """导入模型模块，返回已注册的表数量。

    这个导入的价值全在**副作用**上：不导入模型模块，`db.create_all()` 就看不到任何表
    （它读的是 `db.metadata`，而表定义是在模型类被定义时挂上去的）。
    所以它看起来像"未使用的导入"，实际是必需的——
    这里通过 `models.db` 显式取一次表数量，既说明意图，也让静态检查不再误报。
    """
    from . import models

    return len(models.db.metadata.tables)


def _ensure_missing_columns(app: Flask) -> None:
    """给**已经存在**的表补上模型里新增的列。

    为什么必须要有这一步：`db.create_all()` 只负责建表，**不会给已有的表加列**。
    升级代码后，旧数据库会因为缺少新列而在**每一次查询**上直接报错
    （实测：加了 `lesson_plans.step_rounds` 之后，老库上所有教案查询全部炸掉，
    整个教学功能不可用）。而测试用的都是每次新建的临时库，永远碰不到这一层。

    表结构稳定之后应该换成 Flask-Migrate/Alembic；在那之前，这个检查是
    "改完模型还能正常升级"的底线。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # 新建的表由 create_all 负责
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            column_type = column.type.compile(db.engine.dialect)
            db.session.execute(
                text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}")
            )
            added.append(f"{table.name}.{column.name}")

    if added:
        db.session.commit()
        app.logger.warning("已自动补上缺失的列：%s", "、".join(added))


def _fail_orphan_turns(app: Flask) -> None:
    """把上次进程留下的 pending / running 回合标成失败。

    **为什么必须有这一步**：
        这些回合跑在**进程内的线程池**里（见 `services/runtime.py`）。
        所以"关掉页面"完全不影响它们——这是对的，也正是我们想要的后台运行。
        但**服务一重启，线程就没了**，而数据库里的状态还停在 running。

        不处理的话，界面会永远显示"正在规划中"，用户看到那个状态也就不敢再点
        "重新生成"——课程就此卡死，而且**看不出为什么**（因为确实还有一条
        running 的记录摆在那里）。

    **判成失败是安全的那一侧**：
        后台任务本来就不可恢复。标成失败之后用户能重新生成；
        留在 running 则什么都做不了。
    """
    from datetime import datetime, timezone

    from .models import Turn

    stuck = Turn.query.filter(Turn.status.in_(("pending", "running"))).all()
    if not stuck:
        return

    for turn in stuck:
        # 写清"为什么失败"。空着的话用户会以为系统坏了，
        # 而其实只是需要重新点一次。
        turn.status = "failed"
        turn.error = "服务重启，这个任务中断了。重新生成一次即可。"
        turn.finished_at = datetime.now(timezone.utc)
    db.session.commit()
    app.logger.warning("已把 %d 个被重启打断的回合标为失败", len(stuck))


def _disable_api_caching(app: Flask) -> None:
    """接口响应一律不缓存。

    否则"生成完再查一次"可能命中浏览器或中间层缓存，拿回上一次的空结果——
    表现就是"明明提示已生成，界面却什么都没有"。
    SSE 端点自己设了 Cache-Control，这里不覆盖它。
    """

    @app.after_request
    def _no_store(response):  # noqa: ANN001
        if request.path.startswith("/api/") and not response.headers.get("Cache-Control"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _register_error_handlers(app: Flask) -> None:
    """接口统一返回 JSON，页面才返回 HTML。"""

    def wants_json() -> bool:
        return request.path.startswith("/api/")

    @app.errorhandler(404)
    def not_found(error):  # noqa: ARG001
        if wants_json():
            return jsonify({"ok": False, "error": "接口不存在"}), 404
        return "页面不存在", 404

    @app.errorhandler(500)
    def server_error(error):  # noqa: ARG001
        db.session.rollback()
        if wants_json():
            return jsonify({"ok": False, "error": "服务器内部错误，请看终端日志"}), 500
        return "服务器内部错误", 500
