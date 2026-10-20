"""记住窗口的位置与大小：下次打开还回到原处。

**为什么这件事值得单独一个模块**：
    一个窗口每次都在屏幕正中打开，是"网页套了个壳"的味道；而它记得你上次
    放哪儿、多大，才是"这是一个客户端"的第一印象。这条差别不需要用户学任何东西，
    也不需要界面里多一个开关——它就该自己发生。

存哪儿：`instance/window.json`，与 `.env` 分开放。
    为什么不塞进 `.env`：那一份是**这台机器怎么连模型**（密钥、端点），
    手写的、带注释、用户会打开来看。窗口几何是程序自己写的运行时状态，
    混进去会把手写文件弄乱。
    （Qt 版有一个 `instance/preferences.json` 存主题与字号，那是"这个人怎么看界面"；
    窗口位置属于"这个窗口上次什么样"，是第三类，所以它有自己的一份。）

三条必须说清楚的行为：

    ① **存的永远是"普通状态"的几何**。最大化时窗口尺寸等于整个屏幕，
       把它存下来，下次"还原"就没有可还原的尺寸了——窗口会卡在满屏。
       所以最大化期间收到的尺寸变更一律忽略（见 `main.py` 的 `_GeometryTracker`）。

    ② **看不见的几何会被拉回来**。这是这一块最要紧的健壮性：
       用户把窗口拖到副屏，然后拔掉那台显示器，记录下来的坐标就落在
       一个**不存在的屏幕**上。不做检查的话，下次打开窗口在屏幕外——
       表现为"点了没反应"（其实开了，只是看不见）。判据见
       `MIN_VISIBLE_RATIO` 与 `MAX_OVERHANG`（都在 `clamp_to_screens` 里用）。

    ③ **坏文件不致命**。文件被手改坏、被别的进程写了一半，都只是"这次用默认值"，
       绝不是"打不开"。所以 `load()` 里所有异常都吞掉，返回一份默认几何。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger("crew.window")

#: 默认尺寸。与 `main.py` 里原来的硬编码值一致——首次启动的样子不该因为
#: "加了记忆功能"而变化。
DEFAULT_WIDTH = 1360
DEFAULT_HEIGHT = 900

#: 允许的最小尺寸。与 `create_window(min_size=...)` 保持一致：
#: 面板窄于这个宽度时，左栏 + 正文 + 进度尺就没法共存了。
MIN_WIDTH = 1024
MIN_HEIGHT = 680

#: 窗口至少要有这么大比例的面积落在某块屏里，才算"还看得见"。
#: **用比例而不是固定像素数**——这是第一版写错的地方：固定 240×160 对一个
#: 1200×800 的窗口来说只占 4%，而那种"只在角上露出一点"的窗口，
#: 用户既拖不动它、也想不起来它还在。实测该值 0.35 时：
#: 一个 1200 宽的窗口至少要露出 420px 才算数。
MIN_VISIBLE_RATIO = 0.35

#: 允许窗口最多探出屏幕边缘多少（逻辑像素）。
#: 为什么允许"探出去"而不是要求完整可见：把窗口往边上挪一点、留一条边在屏幕外
#: 是很常见的用法（尤其是想让它别挡住别的东西时）。强行拉回来会让人觉得
#: "这个窗口自己会动"。所以只把**探出太多的**推回来。
#:
#: 它和 `MIN_VISIBLE_RATIO` 的分工容易看错，写清楚：那一条按**窗口尺寸的比例**判，
#: 这一条按**像素数**判。于是窗口越大、比例判据越严——窗口短边 ≥ 686 时
#: `0.35 × 686 > 240`，"可见比例不够"总是先一步命中并回到默认位置，
#: 推回那一段根本不会被执行。只有**贴着最小尺寸**的窗口（短边 680~686）
#: 才会真的走到推回。测试 `test_position_protruding_too_far_is_pushed_back`
#: 就是为这件事存在的，别把那条用例里的尺寸"顺手调整齐"。
MAX_OVERHANG = 240

#: 尺寸超过屏幕这个比例时，认为这是"最大化/贴边"留下的尺寸，不当作普通状态存。
#: 这个判断只在**没有收到过 maximized 事件**时兜底用（比如开机时窗口就是最大化的、
#: 或者事件在订阅之前就已经发生过）。宁可少记一次尺寸，也不要把满屏尺寸记成默认。
SUSPICIOUS_SCREEN_RATIO = 0.92


@dataclass(frozen=True)
class WindowGeometry:
    """一个窗口的几何。`maximized` 单独记，因为它必须在**施加尺寸之后**再设。"""

    x: int
    y: int
    width: int
    height: int
    maximized: bool = False


def state_path() -> Path:
    """状态文件的位置。

    `instance/` 与数据库、日志同一个目录：那是这个应用**所有运行时产物**的家，
    备份或清理时只要看一个地方。

    目录本身由 `data_dir()` 决定而不是"项目根"：装成程序之后程序在
    `C:\\Program Files` 下是只读的，而窗口位置显然得写进用户目录。
    """
    from app.config_desktop import data_dir

    return data_dir() / "instance" / "window.json"


def default_geometry(screens: list) -> WindowGeometry:
    """首次启动时的默认几何：尽量居中。

    `screens` 是 `webview.screens()` 的结果（每个有 `.x/.y/.width/.height`）。
    拿不到屏幕信息（空列表）时就用 (0,0) 加默认尺寸——那正是 `create_window`
    自己会做的事，所以"取不到屏幕"不改变行为。
    """
    if not screens:
        return WindowGeometry(0, 0, DEFAULT_WIDTH, DEFAULT_HEIGHT)

    # 用第一块屏。多屏时"主屏"是哪一个，pywebview 没告诉我们也管不着：
    # 用户第一次打开之后，位置就被记下来了，第二次不会再走这条默认路径。
    screen = _primary(screens)
    width = min(DEFAULT_WIDTH, _screen_width(screen))
    height = min(DEFAULT_HEIGHT, _screen_height(screen))
    x = _screen_x(screen) + max(0, (_screen_width(screen) - width) // 2)
    y = _screen_y(screen) + max(0, (_screen_height(screen) - height) // 3)
    return WindowGeometry(x, y, width, height)


def load(screens: list) -> WindowGeometry:
    """读出上次的几何并**校验**；没有记录 / 读坏了 / 越界了，都回到默认值。"""
    fallback = default_geometry(screens)
    path = state_path()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback          # 第一次打开：正常路径，不用记日志
    except (OSError, ValueError) as exc:
        # 读不了或不是合法 JSON：**不致命**，回默认值就行
        log.warning("窗口状态文件读不了（%s），这次用默认位置：%s", exc, path)
        return fallback

    if not isinstance(raw, dict):
        log.warning("窗口状态文件不是一个对象，这次用默认位置：%s", path)
        return fallback

    geometry = _parse(raw, fallback)
    return clamp_to_screens(geometry, screens, fallback)


def save(geometry: WindowGeometry) -> None:
    """写回状态。失败只记日志——**退出路径上不该因为写不进去而报错**。"""
    path = state_path()
    payload = {
        "x": geometry.x,
        "y": geometry.y,
        "width": geometry.width,
        "height": geometry.height,
        "maximized": geometry.maximized,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False 是为了让手改这个文件的人看懂（虽然一般是程序写的）；
        # 值全是数字与布尔，其实无所谓，写清楚只是免得下次有人问。
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        log.info("已记住窗口位置：%dx%d @ (%d,%d)%s",
                 geometry.width, geometry.height, geometry.x, geometry.y,
                 "（最大化）" if geometry.maximized else "")
    except OSError as exc:
        log.warning("窗口状态没写进去（%s）：%s", exc, path)


# ---------------------------------------------------------------------------
# 内部：解析与校验
# ---------------------------------------------------------------------------

def _as_int(value: object) -> int | None:
    """严格取整数。**不接受** `True`——它是 `int` 的子类，会静默变成 1。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _parse(raw: dict, fallback: WindowGeometry) -> WindowGeometry:
    """从 JSON 对象里取四个数字与一个布尔，缺哪项就用默认值补哪项。"""
    numbers: dict[str, int] = {}
    for field, default in (("x", fallback.x), ("y", fallback.y),
                           ("width", fallback.width), ("height", fallback.height)):
        parsed = _as_int(raw.get(field))
        if parsed is None:
            if field in raw:
                log.warning("窗口状态里的 %s 不是整数（%r），改用默认值", field, raw[field])
            numbers[field] = default
        else:
            numbers[field] = parsed

    maximized = raw.get("maximized")
    return WindowGeometry(
        x=numbers["x"], y=numbers["y"],
        width=numbers["width"], height=numbers["height"],
        maximized=bool(maximized) if isinstance(maximized, bool) else False,
    )


def clamp_to_screens(geometry: WindowGeometry, screens: list,
                     fallback: WindowGeometry) -> WindowGeometry:
    """把几何拉回"看得见"的范围。

    两件事分开做，因为它们的失败方式不同：

        · **尺寸**：比屏幕还大的窗口，标题栏会被推到屏幕外，用户没法拖它。
          所以先按屏幕收窄（但不低于最小值——太小的屏幕就让它超一点，
          那比给一个窄到没法用的窗口强）。
        · **位置**：窗口可能整个落在没有屏幕的地方（拔掉副屏）。这时按
          **重叠比例**判断它还看不看得见（见 `MIN_VISIBLE_RATIO`）；
          看不见就放回默认位置，看得见但探出边缘太多就推回来
          （允许少量探出，见 `MAX_OVERHANG`）。
    """
    if not screens:
        return geometry

    screen = _best_screen(geometry, screens)

    max_width = max(MIN_WIDTH, _screen_width(screen))
    max_height = max(MIN_HEIGHT, _screen_height(screen))
    width = min(max(geometry.width, MIN_WIDTH), max_width)
    height = min(max(geometry.height, MIN_HEIGHT), max_height)

    x, y = geometry.x, geometry.y
    visible = _visible_area(x, y, width, height, screen)
    if visible < width * height * MIN_VISIBLE_RATIO:
        # 看不见（或者只在一个角上露出一点）：不再猜它想去哪，回到默认位置。
        log.warning("上次记的窗口位置 (%d,%d) 只剩 %d 像素可见，回到默认位置",
                    x, y, visible)
        return replace(fallback, maximized=geometry.maximized)

    # 位置与大小都保留，只把"探出太多"的推回来。
    # `min(...)` 与 `max(...)` 的顺序很关键：先按左/上边界夹，再按右/下夹。
    # 屏幕比窗口还小时（极窄的多屏配置）两个边界会交叉，此时结果是
    # 左/上边界——那意味着"贴住屏幕左/上角"，比把窗口推到屏幕外好。
    left = _screen_x(screen)
    top = _screen_y(screen)
    right = left + _screen_width(screen)
    bottom = top + _screen_height(screen)

    x = min(max(x, left - width + MAX_OVERHANG), right - MAX_OVERHANG)
    y = min(max(y, top), bottom - MAX_OVERHANG)

    return replace(geometry, x=int(x), y=int(y), width=int(width), height=int(height))


def looks_like_fullscreen(width: int, height: int, screens: list) -> bool:
    """尺寸大到像是"最大化/贴边"留下的吗？

    只在**没有收到过 `maximized` 事件**时兜底用（见模块顶部的说明）。
    多屏情况下取**最大**的那块屏来比：一块 4K 副屏不该让笔记本屏上的窗口
    被误判成最大化。
    """
    if not screens:
        return False
    tallest = max(_screen_height(screen) for screen in screens)
    widest = max(_screen_width(screen) for screen in screens)
    return (height >= tallest * SUSPICIOUS_SCREEN_RATIO
            and width >= widest * SUSPICIOUS_SCREEN_RATIO)


def _screen_x(screen) -> int:
    return int(getattr(screen, "x", 0))


def _screen_y(screen) -> int:
    return int(getattr(screen, "y", 0))


def _screen_width(screen) -> int:
    return int(getattr(screen, "width", DEFAULT_WIDTH))


def _screen_height(screen) -> int:
    return int(getattr(screen, "height", DEFAULT_HEIGHT))


def _primary(screens: list):
    """挑一块当作"主屏"。

    pywebview 的 `Screen` 没有"是不是主屏"这个字段，所以只能推断。
    **判据是"哪块屏包含坐标原点 (0,0)"**：Windows 上主屏的左上角恒为 (0,0)，
    其它屏的坐标是相对主屏的偏移——可以是负数（副屏摆左边）。

    第一版用的是"最靠左上"（`min(x, y)`），那是**错的**：副屏摆在主屏左边时
    它的 x 是负数，`min` 会选中副屏。这条错误由
    `test_default_picks_the_primary_screen_not_the_leftmost_negative` 抓出来。

    没有屏包含原点时（理论上不会发生）退回到"离原点最近的那块"，
    至少能给出一个确定的结果，而不是随机取第一个。
    """
    for screen in screens:
        if (_screen_x(screen) <= 0 < _screen_x(screen) + _screen_width(screen)
                and _screen_y(screen) <= 0 < _screen_y(screen) + _screen_height(screen)):
            return screen
    return min(screens, key=lambda screen: (
        abs(_screen_x(screen)) + abs(_screen_y(screen)),
    ))


def _visible_area(x: int, y: int, width: int, height: int, screen) -> int:
    """窗口与这块屏的重叠面积（逻辑像素）。"""
    left = max(x, _screen_x(screen))
    top = max(y, _screen_y(screen))
    right = min(x + width, _screen_x(screen) + _screen_width(screen))
    bottom = min(y + height, _screen_y(screen) + _screen_height(screen))
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def _best_screen(geometry: WindowGeometry, screens: list):
    """与窗口重叠最多的那块屏；都不重叠时用主屏。"""
    best = None
    best_area = 0
    for screen in screens:
        area = _visible_area(geometry.x, geometry.y, geometry.width, geometry.height,
                             screen)
        if area > best_area:
            best, best_area = screen, area
    return best if best is not None else _primary(screens)
