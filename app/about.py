"""应用自身的信息：名字、副标题、版本、数据落在哪。

为什么"版本"要**从 README 里读**，而不是写一个 `__version__` 常量：
    这东西最终是**连文件夹一起拷给别人**的，README 是唯一会被顺手改的地方。
    写死一个常量，过两天就会出现"README 写着 v4、界面上写着 v1.0"这种情况，
    而两处都不是真正的版本。读不到就如实说"本地版"。

为什么单独一个模块、而不是塞进 `app/api/settings.py`：
    这些值不止"关于"那一处要看（窗口标题、日志启动行也要），
    而它们跟"怎么连模型"没有关系——放一起会让设置接口顺便 import 一堆无关的东西。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

APP_NAME = "CREW"
APP_SUBTITLE = "学习工作台"

#: 读 README 失败时的兜底。**不要**把它当版本号来源——README 才是。
FALLBACK_VERSION = "CREW V4 · 本地版"


def _readme_title(readme: Path) -> str | None:
    """README 的一级标题（`# 开头的那一行`）。读不到返回 None。"""
    try:
        text = readme.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    return None


def app_version() -> str:
    """版本串，比如 `CREW V4 · 学习工作台`。

    README 在运行期基本不会变，所以缓存；但缓存的是**字符串**而不是 Path，
    免得把某个临时目录记住。
    """
    from .config import BASE_DIR

    return _cached_version(str(BASE_DIR / "README.md"))


@lru_cache(maxsize=8)
def _cached_version(readme_path: str) -> str:
    return _readme_title(Path(readme_path)) or FALLBACK_VERSION


def project_dir() -> Path:
    from .config import BASE_DIR

    return BASE_DIR


def _database_location(config) -> str:
    """数据库在哪（给人看的）。

    SQLite 的 URI 是 `sqlite:///C:/...` 这种形状，直接把整个 URI 摊给用户看没
    意义——他要的是路径。换 MySQL 时也如实说"不在本机文件里"，而不是把
    `mysql+pymysql://用户:密码@主机/库` 原样贴出来（那里面**有密码**，
    而这个信息会出现在界面上）。
    """
    uri = str(config.SQLALCHEMY_DATABASE_URI)
    if uri.startswith("sqlite:///"):
        return uri[len("sqlite:///"):]
    return f"{uri.split('://', 1)[0]}（不在本机文件里，见 .env 的 DATABASE_URL）"


def info() -> dict:
    """给"关于 / 文件位置"用的信息（都是本机路径，没有敏感内容）。

    程序目录与数据目录**分开报**：便携用法下两者相同（数据就在项目里），装成
    程序之后就不同了（程序在安装目录、数据在用户目录）。用户排查"我的课程去哪了"
    的时候，这一行是第一个要看的东西，所以别把它们合成一个字段。
    """
    from .config_desktop import DesktopConfig, data_dir

    project = project_dir()
    data = data_dir()
    return {
        "name": APP_NAME,
        "subtitle": APP_SUBTITLE,
        "version": app_version(),
        "project_dir": str(project),
        "data_dir": str(data),
        "instance_dir": str(data / "instance"),
        "lecture_dir": str(DesktopConfig.LECTURE_DIR),
        "log_file": str(DesktopConfig.LOG_FILE),
        "database": _database_location(DesktopConfig),
    }


__all__ = [
    "APP_NAME",
    "APP_SUBTITLE",
    "FALLBACK_VERSION",
    "app_version",
    "info",
    "project_dir",
]
