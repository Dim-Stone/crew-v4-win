"""运行状态接口：前端启动时检查环境是否就绪，以及把页面侧的报错写进日志。"""

from __future__ import annotations

from flask import Blueprint, current_app, request

from ..config import Config
from ._common import fail, ok

bp = Blueprint("meta", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    return ok({
        "llm_ready": Config.llm_ready(),
        "model": Config.LLM_MODEL,
        "database": "sqlite" if uri.startswith("sqlite") else "mysql",
    })


@bp.get("/startup")
def startup_state():
    """启动状态：是不是第一次用（还没配密钥）、这次建了哪些目录。

    为什么单独一个接口而不是塞进 `/api/health`：
        health 是"服务活着吗"的探针，前后端都在用；把它和"要不要引导用户"
        绑在一起，任何一次探针调用都会变成"引导逻辑是否被触发"的依据。
        两件事分开，各自可以单独改。
    """
    from ..startup import first_run_state

    dirs = (current_app.extensions.get("startup") or {}).get("dirs_created", [])
    return ok(first_run_state(dirs))


@bp.post("/client-log")
def client_log():
    """页面把异常报回来，写进服务端日志。

    为什么需要：前端出错的现场在浏览器里，而用户看到的现象只是"卡住了"。
    没有这条通道，排查只能靠猜——"课堂页停在讲课中、切回对话再进来就好"
    就是这么查出来的（现象说明数据是好的，只有页面没更新）。
    走服务端而不是只打控制台：桌面版没有控制台，日志文件才是唯一能看到的地方。
    """
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    where = str(data.get("where") or "").strip()[:120]
    if not message:
        return fail("message 不能为空")
    current_app.logger.warning("页面报错 [%s]：%s", where or "未知位置", message[:2000])
    return ok()
