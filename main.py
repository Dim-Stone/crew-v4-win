"""CREW 学习工作台 · 启动入口（浏览器内核 + 原生窗口）。

启动方式（三种任选）：
    双击   启动.bat                    —— 用自带的虚拟环境（推荐）
    python main.py                     —— 已经装好依赖时
    .venv_qt\\Scripts\\python.exe main.py

它是怎么跑的（**顺序不能换**）：
    ① 在本机 **127.0.0.1** 的一个空闲端口上，用后台线程起 Flask；
    ② 等它真的能应答了，再用 pywebview 开一个原生窗口指向它；
    ③ 窗口一关，就停掉服务、收尾数据库。

为什么用浏览器内核而不是 Qt 自绘界面（这是一次架构决定，写下来免得后人重走）：
    这套系统的界面里最吃排版的是**公式与讲义**。Qt 内置的富文本引擎
    （`QTextDocument`）只实现 HTML 4 的一个子集，既没有 MathML、也没有
    `text-align` 这类 CSS，连 KaTeX 输出的 HTML+CSS+字体都排不出来。
    试过的替代方案（matplotlib mathtext 光栅化）只能支持 TeX 的一个子集，
    而且数学字体不是论文那套。业界的做法一致：**要真排版就用浏览器内核**——
    而 HTML/CSS/JS 这一套界面**本来就已经写好了**（`app/templates` + `app/static`），
    浏览器版能直接复用，一行都不用重写。

为什么是 pywebview 而不是 PyQt6-WebEngine：
    pywebview 在 Windows 上调用**系统自带的 Edge WebView2 运行时**
    （Win10/11 基本都预装），所以**不用把 Chromium 打进这个包**。
    PyQt6-WebEngine 会把整个 Chromium 带进来（150~200MB 量级），
    而这个项目的分发方式是"把文件夹整体拷走"，体积是要认真对待的成本。

一个刻意的取舍：服务只绑 `127.0.0.1`，**不监听外部地址**。
    这是单人本地工具，没有登录、没有鉴权，绑到 0.0.0.0 等于把
    "用你的密钥调模型"的能力开放给同一局域网里的任何人。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

# 双击 .bat 时工作目录不一定是这里，所以先把项目根放进 sys.path
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from app import window_state  # noqa: E402 —— 必须在 sys.path.insert 之后

log = logging.getLogger("crew.desktop")

WINDOW_TITLE = "CREW · 学习工作台"

#: 优先用这个端口；被占用时自动往后找一个（见 `_pick_port`）。
DEFAULT_PORT = 5000

#: 服务就绪最多等多久（秒）。冷启动要建表、读配置，慢机器上要几秒。
SERVER_READY_TIMEOUT = 30.0


def _setup_logging() -> None:
    """把日志同时写到文件和控制台。

    为什么文件是必需的：这个程序用 `pythonw` 启动时**没有控制台**，
    一次异常如果只打到 stderr，用户除了"它闪了一下就没了"什么也说不出。

    **实现只有一处**：`app/config_desktop.py` 的 `configure_logging()`
    （`run_web.py` 用的是同一个）。这里不再抄第二份——抄一份的后果是迟早分叉，
    比如轮转策略只改了一边，而两个入口本该有完全一样的日志行为。
    """
    from app.config_desktop import configure_logging

    configure_logging()


def _pick_port(preferred: int) -> int:
    """挑一个能用的端口。

    为什么不写死 5000：用户很可能同时开着别的东西占着它，
    而"端口被占用"在窗口里表现为**一片空白**（页面加载失败），
    看不出跟端口有关。所以这里先探测，占用就往后找。
    """
    for candidate in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # 不设 SO_REUSEADDR：这里要的正是"真的没人在用"这个事实
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise RuntimeError(f"{preferred} 起的 20 个端口都被占用了，请先关掉占用的程序")


def _start_server(app, port: int) -> threading.Thread:
    """在后台线程里起 Flask，返回那个线程。

    `use_reloader=False` 是**必须**的：重载器会再 fork 一个进程，
    于是教学回合的状态、数据库连接都会有两个副本（实测过：
    界面里点的生成跑在 A 进程，B 进程看不到，表现为"点了没反应"）。
    """
    thread = threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1", port=port, debug=False,
            threaded=True, use_reloader=False,
        ),
        name="flask",
        daemon=True,   # 主线程一退出就跟着结束，不会留着孤儿进程
    )
    thread.start()
    return thread


def _wait_until_ready(port: int, timeout: float = SERVER_READY_TIMEOUT) -> bool:
    """等服务的端口真的能连上。

    为什么要等、而不是"起完线程就开窗口"：
        窗口比服务先就绪时，WebView 会去请求一个还没监听的端口，
        结果是**空白页**。用户看到的"打不开"其实是抢跑，而不是坏了。
    """
    url_host = "127.0.0.1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((url_host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


class _WindowApi:
    """暴露给页面脚本的一小撮**窗口级**能力（目前只有全屏）。

    pywebview 会把这个对象的公有方法挂到页面的 `window.pywebview.api` 上，
    调用返回 Promise。只放窗口操作，而且刻意**不放**退出/最小化——
    页面脚本不该有能力把程序关掉；那是窗口自己的事（`main` 的收尾逻辑）。

    为什么需要它：课堂页的"全屏"按钮如果直接调浏览器的
    `requestFullscreen()`，在 WebView2 里经常被拒（权限策略 / 非用户手势），
    表现就是用户说的"全屏不了"。走 pywebview 的 `toggle_fullscreen()` 是
    WinForms 层的窗口全屏，稳定得多。
    """

    @staticmethod
    def _window():
        import webview

        windows = getattr(webview, "windows", None) or []
        return windows[0] if windows else None

    def toggle_fullscreen(self) -> bool:
        """切一次窗口全屏。返回是否真的执行了（页面据此决定要不要回退）。"""
        window = self._window()
        toggle = getattr(window, "toggle_fullscreen", None) if window else None
        if toggle is None:
            return False
        toggle()
        return True

    def is_fullscreen(self) -> bool:
        window = self._window()
        return bool(getattr(window, "fullscreen", False)) if window else False


def _open_window(url: str) -> None:
    """开原生窗口。**阻塞**到窗口关闭为止。

    用的是 pywebview 6.x 的 API（实机装的是 6.2.1）：
    `start(private_mode=...)` 与 `create_window(min_size=, text_select=)` 在
    5.x 与 6.x 里都在（`webview/__init__.py` 的 `start`、`webview/window.py`
    的 `create_window`），所以这里没有版本分支。

    `private_mode=False`：私有模式下 WebView2 不保留 cookie 与 local storage，
    而**主题选择就存在 localStorage 里**（见 `base.html` 里那段首屏脚本）——
    用它会导致"每次重开都回到亮色"，是个很招人烦的小毛病。

    窗口的位置与大小由 `window_state` 负责记住（见 `_track_window`）。
    """
    import webview

    # 屏幕列表只问一次：它要经 COM 拿显示器信息，而下面"读几何"和"跟踪事件"
    # 都要用它。问两次不但白花时间，还可能拿到两份不一致的列表。
    screens = _screens()
    state = _load_geometry(screens)

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=state.width,
        height=state.height,
        # x/y 必须**成对**给：pywebview 的 winforms 后端只在两个都不是 None 时
        # 才用 `FormStartPosition.Manual` 摆到那个位置（否则居中）。
        # 所以 x 有值 y 为 None 的半个坐标是没意义的，`window_state` 保证成对。
        x=state.x,
        y=state.y,
        min_size=(window_state.MIN_WIDTH, window_state.MIN_HEIGHT),
        # 上次是最大化关掉的，这次也最大化打开（还原之后是上面那个大小，
        # 因为记下来的尺寸永远是"普通状态"的——见 `window_state` 的说明）
        maximized=state.maximized,
        # 页面里的文字可以选中、复制（讲义要能抄出来）
        text_select=True,
        # 课堂页的"全屏"按钮走这里。
        #
        # 为什么不让页面直接调 `requestFullscreen()`：WebView2 里它经常被拒
        # （权限策略 / 不是用户手势），表现就是"点了没反应"。而窗口级全屏是
        # 走 WinForms 的，稳定得多——这也是用户反馈"全屏不了"的直接原因。
        js_api=_WindowApi(),
    )

    _track_window(window, state, screens)

    # `start()` 在窗口关闭后返回（这就是本函数"阻塞"的含义）。
    webview.start(private_mode=False)


# ---------------------------------------------------------------------------
# 窗口位置与大小的记忆
# ---------------------------------------------------------------------------

def _screens() -> list:
    """当前的屏幕列表。取不到就返回空列表——调用方都按"没有屏幕信息"处理。"""
    try:
        import webview

        return list(webview.screens())
    except Exception:  # noqa: BLE001 —— 拿不到屏幕信息不该阻止窗口打开
        log.debug("拿不到屏幕信息，按单屏默认值处理", exc_info=True)
        return []


def _load_geometry(screens: list) -> window_state.WindowGeometry:
    """读出上次的窗口几何（越界会被拉回来），失败则用默认值。"""
    geometry = window_state.load(screens)
    log.info("窗口几何：%dx%d @ (%d,%d)%s",
             geometry.width, geometry.height, geometry.x, geometry.y,
             "（上次是最大化）" if geometry.maximized else "")
    return geometry


class _GeometryTracker:
    """订阅窗口事件，维护"当前几何"，并在窗口关闭时存下来。

    **为什么用事件而不是在关闭时读一次 `window.width/height/x/y`**：
        关闭那一刻读到的尺寸，在"最大化着关闭"的情况下是整个屏幕的尺寸。
        把它存下来，下次"还原"就没有可还原的大小了——窗口会卡在满屏。
        所以尺寸必须在**普通状态**下采集（见 `on_resized` 的守卫），
        而 `maximized` 事件告诉了我们什么时候不是普通状态。

    pywebview 在 Windows 上确实会发这些事件（winforms.py 里
    `on_resize`/`on_move` 会 set `resized`/`moved`/`maximized`/`restored`），
    所以这里不需要任何猜测性的判断。
    """

    def __init__(self, initial: window_state.WindowGeometry, screens: list) -> None:
        self.geometry = initial
        #: 已经收到过 `maximized` 事件吗？用来决定要不要做那个兜底判断。
        self._saw_maximized_event = initial.maximized
        self._screens = screens

    # ---- 事件处理 ----

    def on_resized(self, width: int, height: int) -> None:
        if self.geometry.maximized:
            return  # 最大化期间的尺寸不是"用户想要的窗口大小"
        if self._looks_fullscreen(width, height):
            # 兜底：开机时窗口就是最大化的、或者事件在订阅之前已经发生过。
            # 宁可少记一次尺寸，也不要把满屏尺寸记成默认。
            log.debug("忽略疑似最大化的尺寸 %dx%d", width, height)
            return
        self.geometry = dataclasses.replace(self.geometry, width=int(width),
                                            height=int(height))

    def on_moved(self, x: int, y: int) -> None:
        if self.geometry.maximized:
            return
        self.geometry = dataclasses.replace(self.geometry, x=int(x), y=int(y))

    def on_maximized(self) -> None:
        self._saw_maximized_event = True
        self.geometry = dataclasses.replace(self.geometry, maximized=True)

    def on_restored(self) -> None:
        self.geometry = dataclasses.replace(self.geometry, maximized=False)

    def on_closed(self) -> None:
        window_state.save(self.geometry)

    def _looks_fullscreen(self, width: int, height: int) -> bool:
        if self._saw_maximized_event:
            return False  # 事件已经告诉我们真实状态了，不用猜
        return window_state.looks_like_fullscreen(width, height, self._screens)


def _track_window(window, initial: window_state.WindowGeometry,
                  screens: list) -> _GeometryTracker:
    """把 tracker 挂到窗口事件上。事件名对不上时**不致命**（只记日志）。"""
    tracker = _GeometryTracker(initial, screens)

    handlers = {
        "resized": tracker.on_resized,
        "moved": tracker.on_moved,
        "maximized": tracker.on_maximized,
        "restored": tracker.on_restored,
        "closed": tracker.on_closed,
    }
    for name, handler in handlers.items():
        event = getattr(window.events, name, None)
        if event is None:
            # 不同 pywebview 版本/后端的事件名不完全一致。缺一个事件只会
            # 让"记忆窗口"这件事退化（比如只记得大小、不记得位置），
            # **不该**让窗口打不开，所以这里只记一条日志。
            log.warning("pywebview 没有 %s 事件，窗口记忆会退化", name)
            continue
        try:
            event += handler
        except Exception:  # noqa: BLE001
            log.warning("订阅 %s 事件失败", name, exc_info=True)

    return tracker


def _shutdown(app) -> None:
    """收尾：把数据库会话关干净。

    为什么**只做这一件事**、不主动去 `shutdown` 那个线程池：
        窗口关掉是用户明确要走。此刻如果有回合正在跑，它多半正卡在一次
        模型调用上（几十秒），用一个"优雅等待"去停它只会让窗口看起来关不掉。
        `flask` 线程与线程池的 worker 都是守护线程，进程一退就走人，
        所以这里要做的只是把 SQLAlchemy 的会话收干净，避免连接泄漏警告。
        （库里的 running 回合会在**下次启动**被 `_fail_orphan_turns` 标成失败——
        这一条本来就有，见 `app/__init__.py`。）
    """
    try:
        from app.extensions import db

        with app.app_context():
            db.session.remove()
    except Exception:  # noqa: BLE001 —— 退出路径上不该再抛异常
        log.exception("关闭数据库会话时出错")

    # 停掉线程池（**不等待**）：窗口已经关了，用户要的是"程序退出"，
    # 而不是"再等几十秒让一个正在跑的模型调用结束"。
    # 不停的话，`ThreadPoolExecutor` 的工作线程会一直挂在解释器退出时，
    # 表现为"关了窗口进程还在"。见 `TurnRuntime.shutdown` 的说明。
    try:
        app.extensions["turn_runtime"].shutdown(wait=False)
    except Exception:  # noqa: BLE001
        log.exception("关闭任务线程池时出错")


def _install_excepthook() -> None:
    """未捕获异常写进日志。

    这里**不弹对话框**：窗口的消息循环在 pywebview 那边，等异常冒到顶层时
    那个循环多半已经不可用了，弹框的时机没法保证。而日志文件里已经有完整栈，
    比一个可能弹不出来的框可靠。
    """
    from app.config_desktop import DesktopConfig

    def hook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.error("未捕获的异常：", exc_info=(exc_type, exc_value, exc_tb))
        # 尽量在控制台也说一句（有控制台时）
        print(f"\n出错了：{exc_type.__name__}: {exc_value}\n"
              f"完整信息已写进日志：{DesktopConfig.LOG_FILE}", file=sys.stderr)

    sys.excepthook = hook


# ---------------------------------------------------------------------------
# 启动闸，以及"怎么把一句话交到用户眼前"
# ---------------------------------------------------------------------------

#: 单实例互斥体的名字。`Local\` 前缀 = "只在当前登录会话内唯一"，
#: 这正是桌面应用要的语义（同一台机器换个用户登录，可以各开一个）。
SINGLE_INSTANCE_MUTEX = "Local\\CREW-v4_win"

#: 互斥体句柄的存放处。**必须留着**：句柄一被回收互斥体就没了，
#: 那道闸门也跟着失效（进程还活着，第二个实例却以为没人开）。
_MUTEX_HANDLES: list = []


def _has_visible_console() -> bool:
    """这个进程有没有一个**看得见**的控制台窗口。

    凡是"要打印给用户看"的地方都得先问一句：正常路径是用 `pythonw.exe` 启动的
    （桌面应用不该带黑框），那个进程没有控制台，`print` 出去没人看得见——
    用户的观感会是"双击了没反应"。

    判据用 Windows 自己的 `GetConsoleWindow()`，**不是** `sys.stdout is None`：
    实测在 pythonw 下 `sys.stdout` 仍是一个 `TextIOWrapper`（写进去被丢掉、
    不报错），拿它当判据会得出"有控制台"的错结论。拿不到这个 API 时返回
    True —— 也就是保持"有控制台"的旧行为，宁可多打印一句也不要吞掉提示。
    """
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return True


def _tell_user(title: str, message: str) -> None:
    """把一句话交到用户眼前：有控制台就打印，没有就弹系统消息框。

    `MessageBoxW` 是标准库 `ctypes` 直接调的，不引入依赖；它是宽字符版本，
    中文不会乱码。弹不出来也不致命——日志里一定留着一份。
    """
    log.info("%s：%s", title, message)
    if _has_visible_console():
        print(f"\n{title}：{message}\n")
        return
    try:
        import ctypes

        MB_ICONINFORMATION = 0x00000040
        MB_SETFOREGROUND = 0x00010000
        ctypes.windll.user32.MessageBoxW(None, message, f"CREW · {title}",
                                         MB_ICONINFORMATION | MB_SETFOREGROUND)
    except Exception:  # noqa: BLE001
        log.exception("消息框弹不出来")


def _already_running() -> bool:
    """这个程序是不是已经开着一个了？

    为什么桌面版必须有这道闸（"多开就多开"在这里是错的）：
        · 两个实例会**各自起一个 Flask**（端口自动往后挪一个），于是同一份
          `instance/crew_v4_win.sqlite3` 被两个进程同时读写；
        · 窗口几何存在同一个 `instance/window.json` 里，谁最后关谁说了算——
          另一个刚摆好的位置会被悄悄覆盖掉；
        · 用户看到的是两个一模一样的窗口，通常会关掉一个，而关掉哪个都不影响
          另一个继续占着端口和数据库。

    判据用 Windows 的**命名互斥体**：它是内核对象，进程一退（包括被任务管理器
    强杀）就自动释放。所以不存在"上次崩溃留下一个假锁、之后再也开不起来"的
    问题——那正是拿锁文件做这件事的经典坑。

    拿不到这个 API 时返回 False（当作"没有别的实例"）：宁可让用户多开一个，
    也不要因为一个环境问题把人挡在门外。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # 句柄是 64 位的，**必须**声明返回类型：默认的 c_int 会把它截断，
        # 于是 `if not handle` 这类判断全都不作数。
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return False
        _MUTEX_HANDLES.append(handle)
        # 必须在 CreateMutexW 之后**立刻**问这一句
        ERROR_ALREADY_EXISTS = 183
        return kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        log.debug('单实例检查不可用，按"没有别的实例"处理', exc_info=True)
        return False


def _mark_started_ok() -> None:
    """记一笔"这台机器上成功启动过"。

    启动器用 `instance\\.started-ok` 判断要不要**保留控制台**：
        第一次启动留在控制台里跑，因为那一次最可能出错（依赖不全、目录不在、
        WebView2 没装），而 `pythonw.exe` 没有任何窗口——出错就是"双击没反应"。
        成功之后写下这个标记，以后都用 pythonw，干干净净。

    写失败不算错：它只影响"下次是否多留一个控制台窗口"，不该阻断启动。
    """
    try:
        # 放在日志旁边：日志目录已经由配置算好（数据目录可以被环境变量搬走），
        # 这里不再自己推一遍路径。局部 import：`main()` 里也是这么导入的，
        # 模块级导入会让 `import main` 顺带把桌面配置（读环境变量）也拉起来。
        from app.config_desktop import DesktopConfig

        marker = Path(DesktopConfig.LOG_FILE).parent / ".started-ok"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.warning("没能写启动标记：%s", exc)


def main() -> int:
    # crewai 的遥测：默认关掉。
    # 它会在第一次跑完 Crew 后问"要不要上传执行轨迹"，并**最多等 20 秒**；
    # 那句话打在标准输出上，而桌面启动时用户看不到控制台，表现为"讲课时卡住 20 秒"。
    # 用 setdefault 留一个后门：想自己开的人只要在系统里设了这个变量。
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "1")

    _setup_logging()
    _install_excepthook()

    from app import create_local_app
    from app.config_desktop import DesktopConfig, ensure_data_dirs

    log.info("CREW 启动，Python %s", sys.version.split()[0])

    # 单实例闸门放在**开服务、开窗口之前**：晚一步的话，第二个实例已经把数据库
    # 连上、端口占好了才开始拒绝自己。判据与原因见 `_already_running`。
    if _already_running():
        _tell_user(
            "已经开着了",
            "CREW 已经有一个窗口在运行（看一下任务栏）。\n\n"
            "同一个程序只开一个：两个实例会同时改同一份数据库和窗口位置记录，"
            "而你分不清哪个窗口是哪个。",
        )
        return 0

    # 数据目录可能还不存在——装成程序之后它落在用户目录下，第一次启动正好是
    # 这种情况。SQLite 的报错是"unable to open database file"，看不出是父目录
    # 没建，所以先建好（便携用法下这两个目录本来就在，mkdir 是空操作）。
    ensure_data_dirs()

    app = create_local_app(DesktopConfig)

    port = _pick_port(int(os.getenv("PORT", str(DEFAULT_PORT))))
    if port != int(os.getenv("PORT", str(DEFAULT_PORT))):
        log.warning("默认端口被占用，改用 %d", port)

    _start_server(app, port)
    if not _wait_until_ready(port):
        message = (f"本地服务在 {SERVER_READY_TIMEOUT:.0f} 秒内没有就绪，"
                   f"窗口没有打开。请查看日志：{DesktopConfig.LOG_FILE}")
        log.error(message)
        print(message, file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/"
    log.info("服务已就绪：%s", url)
    _mark_started_ok()

    try:
        _open_window(url)
    except Exception as exc:  # noqa: BLE001 —— 见下方说明：**任何**失败都退回浏览器
        # 这里故意 catch 得这么宽，理由是"这个应用本来就有一套完整的网页界面，
        # 所以'没有原生窗口'不该等于'不能用'"。具体会撞上的有两类：
        #     · `ImportError`——没装 pywebview（`pip install pywebview` 就有了）；
        #     · WebView2 那一族的异常——pywebview 在 Windows 上靠系统的
        #       **Edge WebView2 Runtime** 画窗口，而它**不是 Windows 自带的**，
        #       精简版系统或长期没更新的机器上可能真的没有。
        # 这两种情况的正确反应是同一个：把同一套界面用系统浏览器打开。
        # 把它们写成一个分支，是因为"哪个异常"对用户毫无意义——
        # 他要知道的是"窗口没开，但东西能用，原因在日志里"。
        log.exception("原生窗口没有打开，改用系统默认浏览器")
        _fallback_to_browser(url, exc)

    log.info("窗口已关闭，开始收尾")
    _shutdown(app)
    log.info("CREW 退出")
    return 0


def _ask_without_console(friendly: str, url: str) -> bool:
    """没有控制台时，用系统消息框问清楚。返回 True 表示"继续在后台跑"。

    为什么是**问**而不是"通知一声就算完"：那个状态（没有窗口、也没有控制台）
    本身没有任何出口——用户看不到提示，也找不到结束的办法。所以这里必须给出
    一个真实的选择：留着它（还要用浏览器里的界面），或者现在就结束。
    `MessageBoxW` 是标准库 `ctypes` 直接调的，不引入依赖；它是宽字符版本，
    中文不会乱码。
    """
    text = (
        f"没有打开原生窗口：{friendly}。\n\n"
        f"已经用系统默认浏览器打开了界面：\n{url}\n\n"
        "现在要在后台继续提供这个界面吗？\n\n"
        "  · 「是」—— 继续运行。用完请从任务管理器结束 pythonw.exe，\n"
        "     或者装上 pywebview / Edge WebView2 之后再启动一次。\n"
        "  · 「否」—— 立刻结束程序（浏览器里的界面会随之停止）。\n\n"
        f"更详细的原因写在日志里：{_log_path()}"
    )
    try:
        import ctypes

        MB_YESNO = 0x00000004
        MB_ICONINFORMATION = 0x00000040
        MB_SETFOREGROUND = 0x00010000
        IDYES = 6
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, "CREW · 没有原生窗口",
            MB_YESNO | MB_ICONINFORMATION | MB_SETFOREGROUND,
        )
        return answer == IDYES
    except Exception:  # noqa: BLE001 —— 连消息框都弹不出来也不能因此崩掉
        log.exception("没有控制台时，消息框也弹不出来")
        return True


def _fallback_to_browser(url: str, exc: BaseException) -> None:
    """用系统默认浏览器打开同一套界面，并一直等到用户表示"用完了"。

    **为什么必须在这里停住**：不开窗口的话，`main()` 走完就 `return 0` 了，
    进程一退，本地服务（跑在守护线程里）跟着消失，`webbrowser.open()` 打开的
    标签页会指向一个已经死掉的端口——用户看到的是"页面打不开"。
    所以这里要顶住，等同于是"用浏览器代替窗口"的那次主循环。

    **"用完了"怎么告诉程序，取决于有没有控制台**（见 `_has_visible_console`）：

        · **有控制台**：打印说明 + 等 Ctrl+C。这是老路，也是正常的那条——
          `启动.bat` 探到没有 pywebview 时会主动换成 `python.exe`。
        · **没有控制台**：打印和 Ctrl+C 都是空的，只能靠消息框说话，
          并把"继续跑还是结束"交给用户选。少了这一步，用户看到的就是
          "双击了没反应"，然后一个看不见的进程一直挂在后台。
    """
    import webbrowser

    friendly = (
        "没有安装 pywebview（装上就有原生窗口："
        ".venv_qt\\Scripts\\python.exe -m pip install pywebview）"
        if isinstance(exc, ImportError)
        else f"原生窗口打不开（{type(exc).__name__}）"
        + "，多半是缺少 Edge WebView2 运行时"
    )

    log.warning("退回浏览器：%s", friendly)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 —— 连浏览器都调不起来时，至少日志里留下痕迹
        log.exception("连系统浏览器都没能调起来")

    if not _has_visible_console():
        if not _ask_without_console(friendly, url):
            log.info("用户选择结束（浏览器模式，且没有控制台）")
            return
        log.info("浏览器模式：在后台继续提供界面，等用户自行结束进程")

    else:
        print(
            f"\n{friendly}。\n"
            f"已改用系统默认浏览器打开：{url}\n"
            f"功能不缺；原因写在日志里：{_log_path()}\n"
            f"服务还在跑——用完按 Ctrl+C 结束（关掉这个控制台窗口也一样）。\n"
        )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def _log_path() -> str:
    """日志文件在哪。只在打印给人看的时候用，所以失败也要给个说法。"""
    try:
        from app.config_desktop import DesktopConfig

        return DesktopConfig.LOG_FILE
    except Exception:  # noqa: BLE001
        return "instance/v4_win.log"


if __name__ == "__main__":
    raise SystemExit(main())
