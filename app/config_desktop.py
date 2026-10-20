"""本地版（本机窗口）专用配置。

与 `app/config.py` 的关系：
    参数来源完全一致（同一个 `.env`、同一套默认值），只是把"怎么跑"换掉——
    数据落在项目目录内、日志写文件而不是只进终端。
    这个类本身**不依赖任何界面库**：它由 `main.py`（pywebview 窗口启动器）使用，
    也照样能被 `run_web.py` 这类纯服务入口使用。

为什么单独一个文件而不改 `config.py`：
    `run_web.py` 仍然按原样跑得起来，本地窗口版只是**多了一个配置类**，
    而不是把原来的拆掉。两套入口共用同一个 `app/`。
"""

from __future__ import annotations

import logging
import logging.handlers  # RotatingFileHandler 在这个子模块里，`import logging` 不带它
import os
from pathlib import Path

from .config import Config

# 程序（代码、模板、静态资源、vendor 的 KaTeX/mermaid）在哪。**只读**的那一半。
DESKTOP_DIR = Path(__file__).resolve().parent.parent


def resolve_data_dir(override: str | None) -> Path:
    """数据目录在哪：有显式指定就用它，否则是项目目录。**纯函数，好测。**

    为什么把这步单独抽出来：下面的 `data_dir()` 要读环境变量，而"读环境变量的
    那一刻"在测试里没法稳定重放——`DesktopConfig` 的几个类属性在**导入时**
    就已经求过值了。纯函数留在外面，断言就能直接喂参数。
    """
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return DESKTOP_DIR


def data_dir() -> Path:
    """数据库、日志、讲义、界面偏好、窗口位置都落在这个目录下。

    **为什么要有它**：这个文件夹是"拷到哪都能跑"的用法，数据放在项目内最省事
    （自包含、好备份）。可一旦装成程序（`.exe` + 安装包），程序会被装到
    `C:\\Program Files\\...`，而那里普通用户**没有写权限**——建库、写日志、存讲义
    会全部失败，而且失败得很散（三处不同的报错，看不出同一个原因）。

    所以解析只有两档：

        ① 环境变量 `CREW_HOME` —— 安装版的启动器会设它（指向用户目录）
        ② 都没有 → **项目目录**（现在的行为，一个字节都不用改）

    **为什么不是"默认落到用户目录、便携用法放一个 `.portable` 标记"**：
        那样"拷文件夹给别人"就变成"拷走一个空壳"——数据留在原来那台机器的
        `%LOCALAPPDATA%` 里了。而"拷文件夹"是这个项目最主要的用法（README 第一句
        写的就是它），所以**便携必须是默认**，反过来让安装版显式指定。
        两种用法于是都成立，也不会让任何现有用户因为一次升级而"看不见自己的课程"。

    它是**启动时**的配置：下面那几个类属性在导入时就把这个值用掉了，
    所以改 `CREW_HOME` 要重启（与 `DATABASE_URL` 同类，不是热更项）。
    """
    return resolve_data_dir(os.getenv("CREW_HOME"))


def ensure_data_dirs() -> None:
    """按数据目录建好 `instance/` 与 `notes/`。

    为什么非做不可：SQLite 的路径指向一个**不存在的目录**时，报的是
    "unable to open database file"——看着像权限问题，其实是父目录没建。
    装成程序之后（数据在 `%LOCALAPPDATA%\\CREW`）第一次启动正好是这种情况。
    """
    for name in ("instance", "notes"):
        (data_dir() / name).mkdir(parents=True, exist_ok=True)


class DesktopConfig(Config):
    """本地窗口版配置：数据（数据库/讲义/日志）默认落在**数据目录**内。"""

    # 默认数据库：<数据目录>/instance/crew_v4_win.sqlite3
    # （`DESKTOP_DATABASE_URL` / `DATABASE_URL` 指向别处时以它们为准）
    SQLALCHEMY_DATABASE_URI = os.getenv("DESKTOP_DATABASE_URL") or (
        f"sqlite:///{(data_dir() / 'instance' / 'crew_v4_win.sqlite3').as_posix()}"
    )
    if SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 1800}

    # 讲义默认落在 <数据目录>/notes/（可用 .env 的 LECTURE_DIR 改到别处）
    LECTURE_DIR = os.getenv("LECTURE_DIR") or str(data_dir() / "notes")

    # 日志：用窗口启动时没有终端可看，出错时必须留得下痕迹
    LOG_FILE = os.getenv("DESKTOP_LOG") or str(data_dir() / "instance" / "v4_win.log")

    # 大模型那四项**不在这里覆盖**：基类把它们放在**元类**的 property 上
    # （见 `app/config.py` 顶部），每次取用都重读环境变量，
    # 所以界面里的"环境配置"改完立刻生效。这里再覆盖一次反而会把热更断掉。

    @staticmethod
    def llm_ready() -> bool:
        # 共用同一套判据（`Config.llm_ready()`），不要再抄一遍——
        # 抄一遍的后果是"同一个机器上两个入口对有没有密钥的判断不一致"。
        return Config.llm_ready()


#: 日志文件的上限：写满就轮转，最多留这么多份备份。
#: 桌面应用是"开着不管"的用法——不轮转这个文件就会一直长，几年下来几百 MB，
#: 而没有人会想起来去清它。5MB × 3 份足够回答"上一次出问题是什么时候"。
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def configure_logging() -> None:
    """把日志同时写到文件和控制台。

    最怕的一件事是"点了没反应"——用 `pythonw` 启动时没有终端，
    异常如果只留在内存里，用户除了"它坏了"什么也说不出。
    所以日志文件是必需品，不是可选项。

    **这里是日志配置的唯一入口**（`main.py` 与 `run_web.py` 都调它）：
    格式、级别、轮转策略只定义一次，两个入口的行为因此必然一致。
    （`main.py` 里原先另抄了一份几乎逐行相同的实现，已经删掉改为调这里。）
    """
    log_path = Path(DesktopConfig.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # RotatingFileHandler 是 FileHandler 的子类，所以这一条判断对它同样有效
    if any(isinstance(handler, logging.FileHandler) for handler in root.handlers):
        return  # 已经配过，避免重复挂 handler（重复挂会让每条日志写两遍）

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
    ))
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
