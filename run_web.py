"""只用系统浏览器的入口。

启动：

    .venv_qt\\Scripts\\python.exe run_web.py

它和 `main.py` 跑的是**同一个应用**（同一个工厂、同一套模板与静态资源），
区别只有一个：谁负责显示。

    这个文件   用你自己的浏览器打开（方便按 F12 看控制台）
    main.py    用 pywebview 开一个原生窗口

一个**必须说清楚**的地方：这里传的是 `DesktopConfig`，和 `main.py` 一样。
    早先它调的是 `create_app()`（默认 `Config`），而两个配置类指向**不同的数据库**
    （`Config` → `instance/crew_v4.sqlite3`，`DesktopConfig` →
    `instance/crew_v4_win.sqlite3`）。那意味着"用浏览器打开"和"用窗口打开"
    会看到两套课程，而且**都不是错误提示**——只是列表莫名其妙地空了或多了几门。
    所以两个入口必须传同一个配置类。

关于 `debug`：
    默认 **关**。Werkzeug 的调试器在出错页面上带一个能执行任意 Python 的交互控制台，
    而这个应用是给人日常用的、不是开发中的服务；虽然只监听 127.0.0.1，
    但"为了看报错而默认开一个代码执行入口"不划算。
    要看详细报错就设 `CREW_DEBUG=1`（或者直接看 `instance/v4_win.log`）。
"""

from __future__ import annotations

import os

from app import create_local_app
from app.config_desktop import DesktopConfig, configure_logging, ensure_data_dirs

configure_logging()
ensure_data_dirs()   # 同 main.py：数据目录不存在时先建好（见那个函数的说明）
app = create_local_app(DesktopConfig)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"CREW 已启动：http://127.0.0.1:{port}/   （Ctrl+C 结束）")
    # threaded=True 是必需的：教学回合在后台线程池里跑，
    # SSE 与其它请求需要并发处理。
    # use_reloader=False：避免重载器起两个进程，导致后台任务状态不一致。
    app.run(
        host="127.0.0.1",
        port=port,
        debug=bool(os.getenv("CREW_DEBUG")),
        threaded=True,
        use_reloader=False,
    )
