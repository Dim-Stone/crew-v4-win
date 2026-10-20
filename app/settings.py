"""环境配置：把 `.env` 里的"怎么连模型"变成界面里能改的东西。

这个模块是 **Qt 版 `qtui/app_settings.py` 的直系后代**（那份已经随 `qtui/`
一起删掉）。搬过来时只做了三件事，其余逻辑一字未改——它跟界面用什么画没有关系：

    · 去掉了对 `app.runtime` 的依赖（那个模块已删，见 `tools/purge_qt.py`）；
      改成 `apply_to_flask_config()`，让 Flask 那份快照跟着对齐。
    · 不再假设"调用方是 Qt 页面"，所以不再往 `os.environ` 以外的地方伸手。
    · 补了 `public_view()`——**掩码只在这里出现一次**，
      免得每个接口各写一遍 `mask()`，写出一个忘了掩的洞。

边界（这条是刻意的，别混）：

    · `.env`（本模块）＝ 这台机器**怎么连模型**（密钥、端点、模型名、温度）。
      存在项目根，**网页版与本机窗口版共用同一份**。
    · `instance/preferences.json` ＝ **这个人怎么看这个界面**（主题、字号）。
      改坏了最多是界面难看。它是前端自己的事，见 `app/static/js/theme.js`。

密钥明文躺在 `.env` 里，这是既有事实（不是本模块引入的）；界面上把它**只显示成
掩码**，是为了别让它出现在截图和录屏里——那不是加密，别当成加密。

三个必须说清楚的行为：

    ① **写盘保留原文件的样子**。`.env` 是手写的，里面有一整段中文注释讲每个参数。
       整份重写会把注释全抹掉（用户下次打开文件就只剩几行键值对），
       所以这里只**就地替换**认得的键，其余原样保留；新键追加到末尾。

    ② **改完立刻生效**。为此 `app/config.py` 里那几个大模型配置被改成了
       "每次取用都重读环境变量"的取值函数。这里只负责 `os.environ` 与
       Flask `app.config` 两边同步更新，界面下一次点击就会用上新值。

    ③ **重启之后仍然生效**，因为值已经写回 `.env` 了。
       （`load_dotenv` 只在启动时跑一次，所以"只改 os.environ 不写文件"
       会得到一个"这次能用、明天打开又没了"的假象。）
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 密钥在界面上**永远只显示**这一串。不是加密，是"别让它在截图里裸奔"。
MASK = "••••••••••••"

# 密钥值里不允许出现这些字符：`\n` 会把后面的行整段变成新的一行
# （`.env` 是逐行的），`#` 之后会被当成注释、引号会被当成包裹符。
# 校验放在这里而不是写盘那里，是为了在**用户点保存的那一刻**就告诉他。
_FORBIDDEN_IN_VALUE = ("\n", "\r", "#")

# 认得的键 → 追加到 .env 末尾时的顺序（只在**新增**该键时用到）。
# 让密钥排在最前：这是最常改、也最需要一眼看到的一项。
KNOWN_KEYS: tuple[str, ...] = (
    "LLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
)

#: 一次保存会写回 `.env` 的四个业务键（不含"另一个可能的密钥键名"）。
LLM_KEYS: tuple[str, ...] = ("LLM_BASE_URL", "LLM_MODEL", "LLM_TEMPERATURE")

_KV_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class SettingsError(ValueError):
    """用户能在界面上看懂并照着改的错误（调用方直接显示 message）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class LLMSettings:
    """一次保存提交上来的四个值（都已经是"要写进 .env 的形式"）。"""

    api_key: str
    base_url: str
    model: str
    temperature: float


def base_dir() -> Path:
    """`BASE_DIR` 的**间接入口**。读配置、定位 `.env`、找 README 都走它。

    为什么不在各处直接 `from .config import BASE_DIR`：那样就没法在测试里
    把"项目根"整体换到临时目录，于是
        · `read_env_file()` 读的是**测试自己的** `.env`（好，因为 env_path 被 patch 了），
        · 而 `current_llm()`（走 `app/config.py`）读的还是**用户真正的** `.env`（坏）。
    这一条不一致本身就是个坑：测试一边在临时文件里写密钥，一边从用户的文件里读，
    断言就会以一种完全看不出原因的方式失败——或者更糟，**看着是过的**。
    所有"项目根在哪"的问题都经过这里，测试只要替换这一个函数就全都一致了。

    （`app/config.py` 里同名的是模块级常量 `BASE_DIR`，不是函数。让那个最底层、
    被所有东西 import 的模块反向依赖本模块会绕成循环导入，所以两边各留一个入口，
    由测试同时替换——见 `tests/test_settings_api.py` 的 `isolated_env` 夹具。）
    """
    from .config import BASE_DIR

    return BASE_DIR


def env_path() -> Path:
    """`.env` 的位置（项目根目录）。"""
    return base_dir() / ".env"


def env_exists() -> bool:
    return env_path().exists()


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def read_env_file() -> dict[str, str]:
    """把 `.env` 读成字典。

    只做最小解析（`KEY=VALUE`，可带 `export`，可被引号包裹）。
    不做变量展开、不做多行值——**一旦支持那些，写回时就不可能"只替换认得的键"**，
    而保留用户手写的内容比支持冷门语法重要得多。
    """
    path = env_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("读 .env 失败：%s", exc)
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _KV_PATTERN.match(line)
        if not match:
            continue  # 注释行、空行、认不出的行：原样留着，不参与解析
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        values[key] = raw
    return values


def api_key_field() -> str:
    """密钥写回哪个键名。

    新写法用 `LLM_API_KEY`，但老用户（以及 Web 版早期文档）用的是
    `DEEPSEEK_API_KEY`，而 `app/config.py` 两个都认。
    这里**跟着文件里已有的那个键走**：文件里有哪个就用哪个，
    都没有才新建 `LLM_API_KEY`。不这么做的话，用户会在自己的 .env 里
    看到两个密钥键，而只有一个生效——那是必然会引起困惑的。
    """
    values = read_env_file()
    if not values.get("LLM_API_KEY") and values.get("DEEPSEEK_API_KEY"):
        return "DEEPSEEK_API_KEY"
    return "LLM_API_KEY"


def current_llm() -> LLMSettings:
    """当前**生效**的配置（不是文件里的原始文本）。

    生效值是"文件里的值 + 代码里的默认值"合成的——用户在 .env 里只填了密钥时，
    界面上应该显示端点与模型的默认值，而不是三个空格子。
    """
    from .config import Config

    return LLMSettings(
        api_key=Config.LLM_API_KEY,
        base_url=Config.LLM_BASE_URL,
        model=Config.LLM_MODEL,
        temperature=float(Config.LLM_TEMPERATURE),
    )


def mask(secret: str) -> str:
    """密钥的展示形式：已配置就显示掩码，没配置就空着。"""
    return MASK if (secret or "").strip() else ""


def is_masked(text: str) -> bool:
    """用户提交上来的密钥是不是"没动过"（界面回填的掩码）。"""
    return (text or "").strip() == MASK


def public_view() -> dict:
    """给界面的那份配置：**密钥只能是掩码**。

    单独一个函数是因为"哪里会漏"这件事只有一处可查：
    任何要往 HTTP 响应里塞配置的代码都必须走它，而不是自己拼一个 dict。
    """
    from .config import SUGGESTED_LLM_MODELS

    settings = current_llm()
    return {
        "api_key": mask(settings.api_key),          # 永不出明文
        "api_key_set": bool((settings.api_key or "").strip()),
        "api_key_field": api_key_field(),           # 保存时写回哪个键
        "base_url": settings.base_url,
        "model": settings.model,
        "temperature": settings.temperature,
        "suggested_models": list(SUGGESTED_LLM_MODELS),
        "env_file": str(env_path()),
        "env_exists": env_exists(),
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def validate(settings: LLMSettings) -> LLMSettings:
    """校验并归一化。有问题就抛 `SettingsError`（message 直接给用户看）。

    这里的每一条都是"写进去就会出问题"的，不是风格偏好：
    空模型名会让每一轮讲课都失败；温度超出 [0,2] 会被服务端直接拒；
    带 `#` 或换行的值会污染 .env 的其余部分。
    """
    key = (settings.api_key or "").strip()
    url = (settings.base_url or "").strip()
    model = (settings.model or "").strip()

    if model == "":
        raise SettingsError("模型名不能为空。DeepSeek 官方端点常用的是 deepseek-flash。")
    if url == "":
        raise SettingsError("接口地址不能为空。DeepSeek 官方端点是 https://api.deepseek.com")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise SettingsError("接口地址要以 http:// 或 https:// 开头。")
    if not (0.0 <= float(settings.temperature) <= 2.0):
        raise SettingsError("温度（temperature）要在 0 到 2 之间。")

    for label, value in (("密钥", key), ("接口地址", url), ("模型名", model)):
        if any(char in value for char in _FORBIDDEN_IN_VALUE):
            raise SettingsError(f"{label}里不能出现换行、# —— 那会把 .env 的其余内容弄坏。")

    return LLMSettings(api_key=key, base_url=url, model=model,
                       temperature=float(settings.temperature))


def parse_temperature(value: object) -> float:
    """把界面交上来的温度变成 float，转不动就抛 `SettingsError`。

    为什么单列一个函数：JSON 里 0.7 可能是数字、也可能是字符串 "0.7"
    （输入框天生给字符串）。两种都得收，而 `float("温暖")` 抛的是
    `ValueError` 而不是 `SettingsError`——那会变成 500，用户看到的
    是"服务器内部错误"，不是"温度要填数字"。这里把它翻译成人话。
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SettingsError("温度（temperature）要填数字，比如 0.7") from exc


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

# 追加新键到 .env 末尾时的顺序：密钥在最前，其余按"越常改的越靠前"排。
# **只在文件里还没有这个键时用到**——已有的键一律就地替换，不动位置。
_APPEND_ORDER: tuple[str, ...] = KNOWN_KEYS


def _format_value(key: str, value: str) -> str:
    """键值对里"值"那一半怎么写。

    只有在值**真的需要**引号时才加引号（含空格的值必须加，否则 dotenv
    会把后半截当注释）。正常情况下保持 .env.example 那种裸写法，
    用户打开文件时看到的东西和他自己写的一样。
    """
    if value == "":
        return ""
    if any(char.isspace() for char in value):
        return f'"{value}"'
    return value


def write_env(updates: dict[str, str]) -> Path:
    """把 `{键: 值}` 就地写回 `.env`（没有的键追加到末尾），返回文件路径。

    值给空串表示"这一项留空"（比如清掉密钥），而不是删除那一行——
    删行会让用户下次打开文件时以为丢了配置，留一行空的反而看得明白。
    """
    path = env_path()
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []

    remaining = updates
    written: set[str] = set()
    out: list[str] = []

    for line in lines:
        match = _KV_PATTERN.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}={_format_value(key, remaining[key])}")
            written.add(key)
        else:
            out.append(line)

    missing = [key for key in _APPEND_ORDER if (key in remaining and key not in written)]
    for key in missing:
        out.append(f"{key}={_format_value(key, remaining[key])}")

    # 结尾补一个换行：文件末尾没有换行时，下一次追加会跟前一行粘在一起。
    text = "\n".join(out).rstrip("\n") + "\n"
    path.write_text(text, encoding="utf-8", newline="")
    log.info("已更新 %s：%s", path, "、".join(sorted(remaining)))
    return path


def _format_temperature(value: float) -> str:
    """0.7 写成 "0.7" 而不是 "0.7000000000000001"（浮点的经典坑）。"""
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


#: 公开别名。`_format_temperature` 这个名字留在模块内部用，
#: 但设置页与测试要按名字引用它——所以给它一个不以下划线开头的名字，
#: 免得"测试只能去戳私有函数"。
format_temperature = _format_temperature


def apply_to_flask_config(app=None) -> None:
    """把"当前环境变量里的"大模型配置同步到 Flask config 上。

    为什么仅改 `os.environ` 还不够：
        `app/config.py` 里那几个值现在是转发到取值函数的 property，
        所以 `Config.LLM_MODEL` 这类读法**已经**能拿到新值了。
        但 Flask 的 `app.config` 是另一份快照：`from_object` 在启动时
        把大写属性逐个拷了进去（`for key in dir(obj): self[key] = getattr(obj, key)`），
        所以别处若按 `current_app.config["LLM_MODEL"]` 读就会读到旧值。
        两份配置不一致是最难查的一类问题，所以在这里一次对齐。
        （顺带一提：`dir()` 是**按字母序**排的，所以那次拷贝的顺序是确定的，
        这里不是"有时候会读到旧值"那种偶发问题——它一定读到旧值。）

    只同步这四个键：数据库、目录、线程数都属于"启动时定下来"的东西，
    运行中改它们（尤其是换数据库）需要重建引擎，不是这里能顺手做的事。
    """
    from .config import Config

    target = app if app is not None else _current_app_or_none()
    if target is not None:
        target.config["LLM_API_KEY"] = Config.LLM_API_KEY
        target.config["LLM_BASE_URL"] = Config.LLM_BASE_URL
        target.config["LLM_MODEL"] = Config.LLM_MODEL
        target.config["LLM_TEMPERATURE"] = Config.LLM_TEMPERATURE
    # 密钥只记"有没有"，不记内容——日志文件不该成为密钥的第二个副本。
    log.info(
        "大模型配置已生效：模型=%s 端点=%s 密钥=%s",
        Config.LLM_MODEL,
        Config.LLM_BASE_URL,
        "已配置" if Config.llm_ready() else "未配置",
    )


def _current_app_or_none():  # noqa: ANN202 —— 返回 Flask | None，避免顶层 import
    """拿当前的 app；不在应用上下文里就返回 None（**不抛异常**）。

    允许在没有上下文时调用，是因为自检脚本会直接调 `save_llm()`——
    它要验的是"值写进 .env 了、`os.environ` 也变了"，
    而不是"当时有没有一个请求在跑"。
    """
    try:
        from flask import current_app

        return current_app._get_current_object()  # type: ignore[attr-defined]
    except RuntimeError:
        return None


def save_llm(settings: LLMSettings, *, api_key_field_name: str | None = None) -> Path:
    """保存大模型配置：校验 → 写 `.env` → 同步进程内环境 → 立刻生效。

    顺序不能换。先写文件再改内存，这样"写盘失败"时内存也不会被改掉——
    否则会出现"这一次能用、重启就没了"的假象。

    `settings.api_key` 是掩码（`is_masked`）时表示"用户没改密钥"，沿用原值。
    """
    clean = validate(settings)
    field = api_key_field_name or api_key_field()

    if is_masked(clean.api_key):
        # 沿用"当前生效的那个密钥"，而不是**重新解析 .env**：
        # `.env` 里可能只有 LLM_API_KEY 与 DEEPSEEK_API_KEY 中的某一个，
        # 而 `Config.LLM_API_KEY` 已经把两种都认了（并且把 .env 与进程环境
        # 两边合起来看了）。再解析一次等于把那份兼容逻辑抄第二遍，
        # 抄错的表现是"保存一次之后密钥没了"——那是最难查的一类故障。
        clean = LLMSettings(
            api_key=current_llm().api_key,
            base_url=clean.base_url,
            model=clean.model,
            temperature=clean.temperature,
        )

    # "另一个键原来是空的吗"要在写盘**之前**问，写完之后文件里读到的是新内容。
    other = "DEEPSEEK_API_KEY" if field == "LLM_API_KEY" else "LLM_API_KEY"

    updates = {
        field: clean.api_key,
        "LLM_BASE_URL": clean.base_url,
        "LLM_MODEL": clean.model,
        "LLM_TEMPERATURE": _format_temperature(clean.temperature),
    }
    # 清空密钥时，把另一个键也一并清掉：两个键都在而只有一个生效
    # 是"改了没反应"的经典来源。
    if clean.api_key == "" and read_env_file().get(other):
        updates[other] = ""

    path = write_env(updates)

    # ---- 让新值立刻生效 ----
    for key, value in updates.items():
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    apply_to_flask_config()
    return path


def clear_llm_key() -> Path:
    """只清掉密钥（"我不想让它留在这台机器上"是最常见的一个需求）。"""
    field = api_key_field()
    path = write_env({field: "", "DEEPSEEK_API_KEY" if field == "LLM_API_KEY" else "LLM_API_KEY": ""})
    os.environ.pop("LLM_API_KEY", None)
    os.environ.pop("DEEPSEEK_API_KEY", None)

    apply_to_flask_config()
    return path


__all__ = [
    "KNOWN_KEYS",
    "LLM_KEYS",
    "LLMSettings",
    "MASK",
    "SettingsError",
    "api_key_field",
    "apply_to_flask_config",
    "base_dir",
    "clear_llm_key",
    "current_llm",
    "env_exists",
    "env_path",
    "format_temperature",
    "is_masked",
    "mask",
    "parse_temperature",
    "public_view",
    "read_env_file",
    "save_llm",
    "validate",
    "write_env",
]
