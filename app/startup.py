"""启动前置：把运行需要的目录建出来，并判断"这是不是第一次用"。

## 为什么需要这个模块

便携包的压缩包里**不保证带上空目录**（`zipfile` 默认不写空目录，很多解压工具
也会跳过它们）。于是别人电脑上解压出来，`instance\\` 与 `notes\\` 可能根本不存在：

    · `instance\\` 不在 → SQLite 连数据库文件都建不出来（它不会替你建目录），
      启动直接失败；
    · `notes\\` 不在   → 讲义写盘失败，而且是"讲到一半才炸"。

所以"目录存在"这件事不能靠打包，必须在**每次启动时**自己保证。
这条比打包脚本更可靠：无论用户是解压、复制、还是从 U 盘拽过来的，都成立。

## 边界

这里只做"建目录 + 报事实"，不碰业务：不建表、不读配置、不初始化数据库。
这样它可以在**创建 app 之前**跑（那时还没有 app context）。
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: 运行期需要的目录（相对项目根）。按"缺了会立刻出事"的顺序。
#: `instance` 里放数据库、日志、界面偏好；`notes` 里放每一轮生成的讲义。
REQUIRED_DIRS: tuple[str, ...] = ("instance", "notes")


def ensure_dirs(base: Path | None = None) -> list[str]:
    """确保目录存在，返回**这次新建的**目录名（已存在的不算）。

    幂等：重复调用没有副作用，所以放在工厂里每进程跑一次是安全的。
    """
    if base is None:
        base = default_base()

    created: list[str] = []
    for name in REQUIRED_DIRS:
        path = Path(base) / name
        if path.is_dir():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 建不出来（只读盘、权限、路径太长）不该在这里抛：
            # 让应用继续起，界面能打开才有机会把"哪里写不进去"告诉用户。
            log.error("建目录失败 %s：%s", path, exc)
            continue
        created.append(name)
        log.info("已创建目录 %s", path)

    return created


def default_base() -> Path:
    """目录应该建在哪儿——**跟着数据目录走**，不是"程序在哪儿"。

    为什么必须这样：
        · 便携用法下两者是同一个（数据就在程序文件夹里）；
        · 但安装版会把 `CREW_HOME` 指到 `%LOCALAPPDATA%`，那时数据库、日志、
          讲义全在那里。如果这里按"程序目录"建，就会在 Program Files 下
          徒劳地建两个目录（还可能因为没权限每次启动都报一遍错）。

    打包实测抓到过这个：跑 `make_portable` 的实测步骤时，包里多出一个空的
    `instance/`——正是本函数按程序目录建出来的，而数据其实在临时 `CREW_HOME` 里。
    """
    try:
        from .config_desktop import data_dir

        return data_dir()
    except Exception:  # noqa: BLE001 —— 没有桌面配置时（比如纯 web 部署）退回项目根
        from .settings import base_dir

        return base_dir()


def llm_configured() -> bool:
    """有没有可用的密钥（不联网，只看配置）。"""
    from .config import Config

    return Config.llm_ready()


def first_run_state(dirs_created: list[str] | None = None) -> dict:
    """给界面的"启动状态"：要不要引导用户去填密钥。

    `env_exists` 只是**提示**，不是判据：密钥也可能来自系统环境变量，
    所以配好没配好一律看 `llm_ready()`。
    """
    from .settings import env_exists

    return {
        "first_run": not llm_configured(),
        "llm_ready": llm_configured(),
        "env_exists": env_exists(),
        "dirs_created": list(dirs_created or []),
    }
