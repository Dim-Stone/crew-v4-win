"""界面偏好：这个人希望界面长什么样。

和另外两份配置的边界（这条是刻意的，别混）：

    · `.env`（`app/settings.py`）      ＝ 这台机器**怎么连模型**（密钥、端点）。手写、带注释。
    · `instance/window.json`           ＝ **这个窗口上次什么样**（位置、大小）。程序自己写。
    · `instance/preferences.json`（本模块）＝ **这个人怎么看界面**（字号、行宽、动效）。
      改坏了最多是界面难看，不会让功能不可用。

**为什么这些项在服务端，而主题不在**：
    主题必须由浏览器**在首屏之前**定下来（否则会先闪一下另一种底色，见
    `base.html` 里那段内联脚本），所以它存在 `localStorage` 里，由前端直接管。
    字号、行宽这些不影响首屏底色的项放服务端，好处是"换了浏览器/清了缓存也还在"，
    而且设置页可以给出**有名字的选项**而不是裸数字。

**为什么每项都用"档位"而不是自由数字**：
    自由输入的结果是用户会把字号调到 26px，然后把界面调坏——而他会认为
    "这个软件排版有问题"。给几个验证过的档位，冲突在源头就没有了。
    每个档位的取值都有理由写在下面。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 档位定义：**只有这一处**写取值范围
# ---------------------------------------------------------------------------
#
# 每一项是 `(值, 界面上的名字, 一句解释)`。解释不是装饰——它是"为什么有这个档位"，
# 用户看不到，但改代码的人需要看到，否则下一个人会随手加一个没验证过的值。

FONT_SIZE_CHOICES: tuple[tuple[int, str, str], ...] = (
    (14, "小", "一屏能看的字最多，适合快速扫读"),
    (15, "标准", "默认。正文 15px，公式与代码按比例跟它走"),
    (16, "大", "长时间阅读更省力"),
    (18, "特大", "投屏、或者离屏幕比较远时用"),
)

CONTENT_WIDTH_CHOICES: tuple[tuple[int, str, str], ...] = (
    (760, "窄", "一行约 30 个汉字，最不容易看串行"),
    (920, "标准", "默认。讲义与对话的常用宽度"),
    (1200, "宽", "配合大屏，一屏能看到更多内容"),
)

LINE_HEIGHT_CHOICES: tuple[tuple[float, str, str], ...] = (
    (1.6, "紧凑", "同样一屏能多放几行"),
    (1.75, "标准", "默认。中文正文在这个行距下最舒服"),
    (1.95, "宽松", "行与行分得开，适合公式多的讲义"),
)

DENSITY_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("comfortable", "标准", "默认。卡片与区块之间留够空白"),
    ("compact", "紧凑", "同样一屏能看到更多课程与消息，适合小屏幕"),
)

#: 默认值。**必须**是上面某一个档位的值——有一条测试盯着这件事，
#: 因为"默认值不在选项里"会让设置页打开时没有任何一项被选中（看着像坏了）。
DEFAULTS: dict[str, Any] = {
    "font_size": 15,
    "content_width": 920,
    "line_height": 1.75,
    "density": "comfortable",
    "reduce_motion": False,
}

#: 允许的取值。从上面的档位表算出来，不手写第二遍。
CHOICES: dict[str, tuple[Any, ...]] = {
    "font_size": tuple(value for value, _, _ in FONT_SIZE_CHOICES),
    "content_width": tuple(value for value, _, _ in CONTENT_WIDTH_CHOICES),
    "line_height": tuple(value for value, _, _ in LINE_HEIGHT_CHOICES),
    "density": tuple(value for value, _, _ in DENSITY_CHOICES),
    "reduce_motion": (True, False),
}

#: 旧版（Qt 界面）的偏好文件里可能有这些键。它们**不再生效**，
#: 但读到时要说一句——否则用户会以为"我的设置被吃了"。
_LEGACY_KEYS = ("theme", "sidebar_width", "appearance", "diagram_engine",
                "notes_dir", "window_x", "window_y", "window_w", "window_h")


@dataclass
class Preferences:
    """一份界面偏好。字段名就是 JSON 里的键名。"""

    font_size: int = DEFAULTS["font_size"]
    content_width: int = DEFAULTS["content_width"]
    line_height: float = DEFAULTS["line_height"]
    density: str = DEFAULTS["density"]
    reduce_motion: bool = DEFAULTS["reduce_motion"]
    #: 读文件时发现的、已经不再生效的旧键。不写回文件，只在界面上提一句。
    legacy_keys: tuple[str, ...] = field(default=(), compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("legacy_keys", None)   # 这是"读到过什么"，不是一项设置
        return data


def preferences_path() -> Path:
    """偏好文件的位置。与数据库、日志、窗口状态同一个 `instance/` 目录。

    用的是**数据目录**（`app/config_desktop.py` 的 `data_dir()`），不是程序目录：
    装成程序之后程序在 `C:\\Program Files` 下是只读的，偏好——和数据库、日志
    一样——必须写到用户目录去。
    """
    from .config_desktop import data_dir

    return data_dir() / "instance" / "preferences.json"


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def load() -> Preferences:
    """读出偏好。文件不存在 / 读坏 / 值非法，都只是"那一项回到默认值"。

    **绝不抛异常**：界面偏好读不出来不该让页面打不开。这是它与 `.env`
    最大的不同——密钥错了会导致功能不可用（必须让用户知道），
    字号错了只是字号不对。
    """
    path = preferences_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Preferences()                 # 第一次：正常路径
    except (OSError, ValueError) as exc:
        log.warning("偏好文件读不了（%s），这次全部用默认值：%s", exc, path)
        return Preferences()

    if not isinstance(raw, dict):
        log.warning("偏好文件不是一个对象，这次全部用默认值：%s", path)
        return Preferences()

    known: dict[str, Any] = {}
    for key, allowed in CHOICES.items():
        if key not in raw:
            continue
        value = _coerce(key, raw[key], allowed)
        if value is None:
            log.warning("偏好里的 %s=%r 不是允许的取值，改用默认值 %r",
                        key, raw[key], DEFAULTS[key])
            continue
        known[key] = value

    legacy = tuple(key for key in _LEGACY_KEYS if key in raw)
    if legacy:
        log.info("偏好文件里有 %d 个旧版（Qt 界面）的键，它们不再生效：%s",
                 len(legacy), "、".join(legacy))

    return Preferences(**known, legacy_keys=legacy)


def _coerce(key: str, value: Any, allowed: tuple[Any, ...]) -> Any | None:
    """把 JSON 里的值对齐到允许的档位；对不上返回 None。

    为什么不能直接 `value in allowed`：JSON 里 `1.0` 与 `1` 是不同的写法，
    而 `True == 1` 在 Python 里成立——那会让 `font_size: true` 被当成 1
    （然后因为 1 不在档位里而被拒，看起来是对的，但换成 `font_size: 14.0`
    就会被接受成 14）。所以布尔与数字要分开处理，数字按"最接近的档位"归一。
    """
    if key == "reduce_motion":
        return value if isinstance(value, bool) else None

    if isinstance(value, bool):
        return None                       # 布尔不能当数字用（见上面的说明）

    if key == "density":
        return value if value in allowed else None

    if not isinstance(value, (int, float)):
        return None

    if value in allowed:
        return value

    # 数值型：取最接近的档位。这样旧文件里写 15.5 之类的值也能落到一个合法档位上，
    # 而不是整项失效。**只对数值型做这件事**——`density` 那种字符串没有"最接近"。
    return min(allowed, key=lambda candidate: abs(candidate - value))


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def save(updates: dict[str, Any]) -> Preferences:
    """就地更新若干项并写盘，返回更新后的完整偏好。

    只接受认得的键：设置页会带上**整个表单**，将来加了新项之后，老版本界面
    提交上来的表单里不会包含新键——那些键保持原值，不该被清掉。
    """
    current = load()
    merged = current.to_dict()

    for key, value in updates.items():
        allowed = CHOICES.get(key)
        if allowed is None:
            log.warning("忽略不认识的偏好项：%s", key)
            continue
        coerced = _coerce(key, value, allowed)
        if coerced is None:
            # 非法值不该让整次保存失败（其它项还是有效的），但要留痕
            log.warning("忽略非法的偏好项：%s=%r（允许：%r）", key, value, allowed)
            continue
        merged[key] = coerced

    path = preferences_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        log.info("已保存界面偏好：%s",
                 "、".join(f"{key}={value}" for key, value in merged.items()))
    except OSError as exc:
        log.warning("偏好没写进去（%s）：%s", exc, path)

    return Preferences(**merged, legacy_keys=current.legacy_keys)


def reset() -> Preferences:
    """恢复默认，并**删掉文件**。

    删而不是写一份默认值进去：这样"恢复默认"之后，用户得到的与"从来没设置过"
    完全一致——包括将来改了默认值时，他会跟着一起变。写死一份当前默认值，
    他就永远停在旧默认上了。
    """
    path = preferences_path()
    try:
        path.unlink(missing_ok=True)
        log.info("已恢复默认界面偏好（删除了 %s）", path)
    except OSError as exc:
        log.warning("偏好文件删不掉（%s），改写一份默认值：%s", exc, path)
        return save(dict(DEFAULTS))
    return Preferences()


# ---------------------------------------------------------------------------
# 给界面看的那一份
# ---------------------------------------------------------------------------

def public_view() -> dict[str, Any]:
    """偏好 + 每个档位的说明。设置页靠它把选项渲染出来。

    把档位表一起发过去（而不是让前端自己写一份）：**取值范围只有一处**。
    写两份的话，改了服务端的默认值而前端还列着旧档位，用户选了就会被静默忽略。
    """
    current = load()
    return {
        "values": current.to_dict(),
        "defaults": dict(DEFAULTS),
        "choices": {
            "font_size": _describe_int(FONT_SIZE_CHOICES),
            "content_width": _describe_int(CONTENT_WIDTH_CHOICES),
            "line_height": _describe_float(LINE_HEIGHT_CHOICES),
            "density": _describe_str(DENSITY_CHOICES),
            "reduce_motion": [
                {"value": False, "label": "跟随系统", "hint": "默认。系统里关了动画，界面就关"},
                {"value": True, "label": "一直关掉", "hint": "不给呼吸点、淡入这些加动效"},
            ],
        },
        "legacy_keys": list(current.legacy_keys),
    }


def _describe_int(choices: tuple[tuple[int, str, str], ...]) -> list[dict[str, Any]]:
    return [{"value": value, "label": label, "hint": hint}
            for value, label, hint in choices]


def _describe_float(choices: tuple[tuple[float, str, str], ...]) -> list[dict[str, Any]]:
    return [{"value": value, "label": label, "hint": hint}
            for value, label, hint in choices]


def _describe_str(choices: tuple[tuple[str, str, str], ...]) -> list[dict[str, Any]]:
    return [{"value": value, "label": label, "hint": hint}
            for value, label, hint in choices]


__all__ = [
    "CHOICES",
    "CONTENT_WIDTH_CHOICES",
    "DEFAULTS",
    "DENSITY_CHOICES",
    "FONT_SIZE_CHOICES",
    "LINE_HEIGHT_CHOICES",
    "Preferences",
    "load",
    "preferences_path",
    "public_view",
    "reset",
    "save",
]
