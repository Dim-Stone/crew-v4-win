"""全局配置。

所有可变项都从环境变量（.env）读取，代码里不写死密钥。
数据库用 DATABASE_URL 一个变量控制：一期 SQLite，将来切 MySQL 只改这一行。

**为什么大模型那几个值要走"取值函数 + property"这一层**（改之前先读完）：
    界面里有"环境配置"（`app/api/meta.py` 读、设置页写 `.env`），
    用户改完密钥／模型后期望**立刻生效**，而不是"改完还要重启"。可是：

        · 类属性是在 `import app.config` 那一刻求值的。`Config.LLM_API_KEY`
          于是被钉死在启动时的环境上——后面再怎么改 `os.environ` 都读不到新值。
        · 更麻烦的是 `build_llm()` 是**每一轮**讲课/备课都调一次
          （见 `app/agents/base.py`）。也就是说"每轮重新读一次"这件事
          本来就已经发生了，唯一挡住热更的就是上面那条类属性。

    所以这里改成"模块级取值函数 + 元类上的 property"：
    `_llm_api_key()` 之类的函数**每次调用都重读**（环境变量 → `.env` 文件），
    `Config.LLM_API_KEY` 只是它的一个转发入口（老代码 `Config.LLM_API_KEY` 一字不改）。

    **为什么还要读 `.env` 文件，而不只是 `os.environ`**：
        `load_dotenv()` 只在 import 那一刻灌一遍进程环境。于是
        "环境变量里没有、而 `.env` 里有"是一个真实存在的空档，而它会让
        设置页保存时把密钥写成空（详见下面 `_dotenv_values` 的长注释）。
        环境变量**仍然优先**，文件只是兜底。

    **为什么不像其它配置那样直接写类属性**：`app/config_desktop.py` 的
    `DesktopConfig` 需要覆盖数据库与讲义目录——它正是靠"类属性覆盖"做到的。
    如果这里写成普通 property，子类就再也覆盖不动了（子类里执行 `os.getenv(...)`
    返回的是字符串，那是正常赋值；但**读取时** `DesktopConfig.LLM_MODEL`
    会去访问类上的 property 对象，而不是实例求值，直接 AttributeError）。
    所以这四个值放在**元类**上：元类的 property 对"类属性访问"生效，
    子类照样能用类属性覆盖别的东西。详见 `_ConfigMeta`。

注意事项：
    · `DEEPSEEK_API_KEY` 这条兼容读法**必须留着**——
      老用户的 .env 可能只有那个变量名。
    · 这几个值属于"每次取用都该重读"的一类；`TURN_MAX_WORKERS` 那种
      "启动时定下来就不再变"的参数保持类属性不变。
    · **在应用运行期间手工编辑 `.env` 不会生效**（环境变量优先，而它没有被重灌）。
      走界面里的"设置 → 模型连接"保存才会立刻生效——那条路会同时更新
      `os.environ`。这是既有行为，不是这次改动引入的。
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env 存在就加载（不存在也能跑，只是没有 API key）
load_dotenv(BASE_DIR / ".env")

#: `.env` 的一行：`KEY=VALUE`，可带 `export` 前缀。
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

#: `.env` 的解析缓存。`key` 是 `(mtime_ns, size)`，变了就重读——
#: 所以 `app/settings.py` 写完文件之后这里自动看到新值，不需要谁清缓存。
_dotenv_cache: dict[str, object] = {"key": None, "values": {}}

# ---- 大模型配置的默认值：**只有这一处**写默认值 ----
# 环境配置表单的初值由 `app/api/meta.py` 提供（界面读它来填输入框），
# 所以默认值散在两边的话，"界面里显示的值"和"实际用的值"迟早对不上。
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
# 默认值按 DeepSeek 官方端点**实测可用**的值填写（deepseek-chat 等旧名在该端点已不存在）
DEFAULT_LLM_MODEL = "deepseek-flash"
DEFAULT_LLM_TEMPERATURE = 0.7
# 单次模型调用的超时（秒）。上游"连接不断、数据不来"时，没有它就会永远停在"讲课中"。
DEFAULT_LLM_TIMEOUT = 180.0
# 下拉框里给出的候选模型。那个端点上实测可用的是这两个；
# 设置页允许手输别的名字，所以它只是"建议"，不是白名单。
SUGGESTED_LLM_MODELS = ("deepseek-flash", "deepseek-v4-pro")


# ---------------------------------------------------------------------------
# 大模型配置的取值函数（每次都重读环境变量，所以界面改完立刻生效）
# ---------------------------------------------------------------------------
#
# **环境变量 → `.env` 文件**的两级回落，这一层是必需的（踩过）：
#     取值函数的本意是"每次取用都重读"，但 `os.environ` 只在进程启动时被
#     `load_dotenv()` 灌过一遍。于是"环境变量里没有、而 `.env` 里有"就成了一个
#     危险的空档：设置页保存时（`app/settings.py` 的"掩码 = 不改密钥"分支）
#     会去读当前生效值，读到空串，然后**把密钥写成空**。
#     这个空档在真实使用里是会出现的：只要用户是在这一台机器上先有 `.env`、
#     后来进程环境里那一项被清掉（或者用 `pythonw` 从资源管理器启动、
#     某些环境下变量没被继承），保存一次就把密钥弄丢了。
#     所以文件是兜底，不是可选读物。
#
# **优先级仍然是环境变量优先**：那是既有行为（`.env` 只是它的来源之一），
# 而且显式设了变量的部署方式必须继续压过文件。

def _dotenv_values() -> dict[str, str]:
    """按需解析项目根的 `.env`，返回 `{键: 值}`。

    为什么要缓存：这几个取值函数是**每一轮讲课都调**的（见 `app/agents/base.py`），
    每次读盘解析一个文件不值得。缓存键用 `(mtime_ns, size)`——两者任一变化就重读，
    所以 `save_llm()` 写完文件之后立刻能读到新值，不需要谁去手动清缓存。

    解析方式与 `app/settings.py` 的 `read_env_file()` **刻意保持一致但各写一份**：
    让 `config`（最底层、被所有东西 import）反过来依赖 `settings` 会绕成循环导入。
    两边的规则都很小（`KEY=VALUE`、可带 `export`、可被引号包裹），并且有测试盯着。
    """
    path = BASE_DIR / ".env"
    try:
        stat = path.stat()
    except OSError:
        _dotenv_cache["key"] = None
        _dotenv_cache["values"] = {}
        return {}

    key = (stat.st_mtime_ns, stat.st_size)
    if _dotenv_cache["key"] == key:
        return _dotenv_cache["values"]  # type: ignore[return-value]

    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        # 文件刚被删/被占用：退回上一次的结果，而不是假装没有配置。
        return _dotenv_cache["values"] or {}  # type: ignore[return-value]

    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if not match:
            continue
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        values[match.group(1)] = raw

    _dotenv_cache["key"] = key
    _dotenv_cache["values"] = values
    return values


def _setting(name: str) -> str:
    """一个配置项的生效值：环境变量优先，其次 `.env` 文件，都没有就是空串。"""
    from_env = os.getenv(name)
    if from_env is not None and from_env.strip():
        return from_env
    return _dotenv_values().get(name, "")


def _llm_api_key() -> str:
    return (_setting("LLM_API_KEY") or _setting("DEEPSEEK_API_KEY")).strip()


def _llm_base_url() -> str:
    return _setting("LLM_BASE_URL").strip() or DEFAULT_LLM_BASE_URL


def _llm_model() -> str:
    return _setting("LLM_MODEL").strip() or DEFAULT_LLM_MODEL


def _llm_temperature() -> float:
    raw = _setting("LLM_TEMPERATURE").strip()
    try:
        return float(raw) if raw else DEFAULT_LLM_TEMPERATURE
    except ValueError:
        # 手写坏了的数字不该让整轮讲课炸掉：退回默认值继续，
        # 设置页保存时会做一次校验并当面告诉用户。
        return DEFAULT_LLM_TEMPERATURE


def _llm_timeout() -> float:
    """单次模型调用最多等多久（秒）。

    为什么必须有：上游偶尔"连接不断、数据不来"——流式生成停在半路，
    既不报错也不结束。没有超时，那一轮就永远停在"讲课中"，
    学生只能看着转圈（实际发生过）。给一个上界，至少能明确失败、可以重试。

    默认 180 秒：正常一轮讲授几十秒；超过三分钟基本就是卡住了。
    """
    raw = _setting("LLM_TIMEOUT").strip()
    try:
        value = float(raw) if raw else DEFAULT_LLM_TIMEOUT
    except ValueError:
        return DEFAULT_LLM_TIMEOUT
    return value if value > 0 else DEFAULT_LLM_TIMEOUT


def _sqlite_url() -> str:
    """默认 SQLite 路径。

    **这里不建目录**（以前会）。原因有两个，都是实测撞出来的：

    ① 它在 `import app.config` 时就被求值（类属性），也就是"建目录"这个副作用
       发生在**任何启动逻辑之前**。于是 `startup.ensure_dirs()` 里"建了哪些目录"
       的报告永远看不到 `instance/`——它早就被建好了，
       而"报告和事实不一致"是最难查的那类问题。
    ② 它按 `BASE_DIR`（程序目录）建，而数据目录可能被 `CREW_HOME` 指到别处。
       打包实测时表现为：跑完实测，包里多出一个空的 `instance/`，
       而真正的数据在临时目录里。

    目录现在由 `app.startup.ensure_dirs()` 统一负责（工厂里、连数据库之前调用），
    它按**数据目录**建，所以便携版和安装版都对。
    """
    return f"sqlite:///{(BASE_DIR / 'instance' / 'crew_v4.sqlite3').as_posix()}"


class _ConfigMeta(type):
    """元类：让 `Config.LLM_MODEL` 这种**类属性直接访问**能触发动态 getter。

    为什么普通 `@property` 不够：
        在普通类上写 `@property`，只有实例（`Config().LLM_MODEL`）会触发 getter；
        而全项目都是当类属性用（`Config.LLM_MODEL`）。在类上直接读普通 property，
        只会拿到 `<property object>` 本身，导致模型名和密钥都变成 property 对象。
        元类上的 property 作用于类本身，因此 `Config.LLM_MODEL` 返回真实字符串。
    """

    @property
    def LLM_API_KEY(cls) -> str:
        return _llm_api_key()

    @property
    def LLM_BASE_URL(cls) -> str:
        return _llm_base_url()

    @property
    def LLM_MODEL(cls) -> str:
        return _llm_model()

    @property
    def LLM_TEMPERATURE(cls) -> float:
        return _llm_temperature()

    @property
    def LLM_TIMEOUT(cls) -> float:
        return _llm_timeout()


class Config(metaclass=_ConfigMeta):
    # ---- 数据库 ----
    # 留空 => SQLite；填 mysql+pymysql://... => MySQL，代码无需改动
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL") or _sqlite_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SQLite 必须允许跨线程（教学回合跑在线程池里）
    if SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 1800}

    # ---- 大模型（实例访问代理，保持类与实例一致） ----
    @property
    def LLM_API_KEY(self) -> str:
        return _llm_api_key()

    @property
    def LLM_BASE_URL(self) -> str:
        return _llm_base_url()

    @property
    def LLM_MODEL(self) -> str:
        return _llm_model()

    @property
    def LLM_TEMPERATURE(self) -> float:
        return _llm_temperature()

    @property
    def LLM_TIMEOUT(self) -> float:
        return _llm_timeout()

    # ---- 运行时护栏 ----
    TURN_MAX_WORKERS = int(os.getenv("TURN_MAX_WORKERS", "2"))
    CONTEXT_RECENT_TURNS = int(os.getenv("CONTEXT_RECENT_TURNS", "6"))

    # ---- 讲义保存 ----
    # 授课内容会自动写成 Markdown 存到这里（一个"工作区"目录）。
    # 相对路径以项目根目录为基准；也可以直接写绝对路径，比如 D:\我的讲义
    LECTURE_DIR = os.getenv("LECTURE_DIR") or str(BASE_DIR / "notes")

    # ---- 其它 ----
    JSON_AS_ASCII = False
    SECRET_KEY = os.getenv("SECRET_KEY", "crew-v4-local-dev")

    @staticmethod
    def llm_ready() -> bool:
        """是否已配置可用的大模型密钥。

        复用 `_llm_api_key()`，**不要**改回 `os.getenv(...)`：
        那个写法只看进程环境，`.env` 里有密钥而环境变量里没有时会误报"未配置"，
        界面上就是顶栏一直挂着"未配置密钥"，而其实能正常讲课。
        （这个判据还有第二个调用方：`app/api/meta.py` 的 `/api/health`。）
        """
        return bool(_llm_api_key())
