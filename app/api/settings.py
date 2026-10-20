"""环境配置接口：读写 `.env` 里的模型连接信息。

为什么这些接口值得存在（而不是"让用户自己去编辑 .env"）：
    `.env` 就在项目根目录，用户想改当然能改。但改完**必须重启**才生效，
    而重启会丢掉正在跑的回合。界面里改完是立刻生效的，所以这是实际差别，
    不是"锦上添花"。

安全边界（本机版的前提，写清楚免得以后被搬上网）：
    服务只监听 127.0.0.1，没有登录、没有 CSRF。`GET` **不下发明文密钥**
    （只给掩码），但 `PUT` 能把密钥写进去——也就是说，**能访问这个端口就等于
    能改这台机器上的密钥**。所以这个服务绝不能监听 0.0.0.0。
"""

from __future__ import annotations

from flask import Blueprint, request

from .. import about as about_mod
from .. import settings as settings_mod
from ._common import fail, ok

bp = Blueprint("settings", __name__, url_prefix="/api/settings")


@bp.get("/about")
def read_about():
    """版本与数据目录。都是本机路径，没有敏感内容。"""
    return ok(about_mod.info())


@bp.get("/llm")
def read_llm():
    """当前生效的模型配置。密钥是掩码。"""
    return ok(settings_mod.public_view())


@bp.put("/llm")
def save_llm():
    """保存模型配置：写 `.env` → 同步进程环境 → 立刻生效。

    请求体里**不带 `api_key`**、带**掩码**、或者带**空串**——三种都表示
    "这次不动密钥"，沿用 `.env` 里原来那个。这一点很重要：页面上回填的就是掩码，
    用户只改了模型名就点保存时，绝不能把密钥覆盖成一串 •。

    "清掉密钥"是**另一个动作**（`DELETE /api/settings/llm/key`），不在这里。
    """
    data = request.get_json(silent=True) or {}

    current = settings_mod.current_llm()

    # 密钥的三种交法，都归结到"这次要不要动密钥"：
    #   · 字段不在请求里           → 不动（只改了模型名之类的）
    #   · 交上来是掩码             → 不动（页面回填的就是掩码）
    #   · 交上来是**空串**         → 也不动
    # 空串不管看起来多像"我要清掉它"，都不该在这里当成删除：页面上的输入框
    # 本来就可能是空的，而"清掉密钥"是**另一个按钮**（DELETE）。
    # 把空串当删除的话，用户只改了一下模型名就会被顺手清掉密钥——
    # 那种故障表现为"讲了半天课，突然说没配置密钥"，且很难联想到这一步。
    raw_key = data.get("api_key", settings_mod.MASK)
    if raw_key is None or str(raw_key).strip() == "":
        raw_key = settings_mod.MASK

    base_url = data.get("base_url", current.base_url)
    model = data.get("model", current.model)

    if "temperature" in data:
        try:
            temperature = settings_mod.parse_temperature(data["temperature"])
        except settings_mod.SettingsError as exc:
            return fail(exc.message)
    else:
        temperature = current.temperature

    incoming = settings_mod.LLMSettings(
        api_key=str(raw_key),
        base_url=str(base_url or ""),
        model=str(model or ""),
        temperature=temperature,
    )

    try:
        settings_mod.save_llm(incoming)
    except settings_mod.SettingsError as exc:
        # 校验类错误是"用户照着改就行"，所以是 400 + 人话，不是 500。
        return fail(exc.message)

    # 回读一次：让界面拿到的就是**真正在用的值**（默认值补齐后），
    # 而不是它自己刚提交上来的那半份。
    return ok(settings_mod.public_view())


@bp.post("/llm/test")
def test_llm():
    """真连一次模型端点，确认密钥能用。

    为什么值得专门做：第一次用的人填完密钥，唯一的反馈是"保存成功"——
    而"保存成功"只说明写进了文件，不说明这个密钥是对的。等他讲了一节课
    卡住再回来查，成本高得多。

    做法：请求 `{base_url}/models`。便宜、快、几乎不消耗额度，
    而且 401 与网络不通能清楚区分开（这两种情况用户要做的事完全不同）。
    """
    import json as json_module
    import urllib.error
    import urllib.request

    data = request.get_json(silent=True) or {}
    current = settings_mod.current_llm()

    base_url = str(data.get("base_url") or current.base_url or "").strip().rstrip("/")
    if not base_url:
        return fail("先填接口地址，比如 https://api.deepseek.com")

    candidate = data.get("api_key")
    if not candidate or settings_mod.is_masked(str(candidate)):
        candidate = current.api_key
    key = str(candidate or "").strip()
    if not key:
        return fail("还没有填密钥")

    # 只认 http/https：`file://` 之流不该被"测试连接"带出去
    if not base_url.lower().startswith(("http://", "https://")):
        return fail("接口地址要以 http:// 或 https:// 开头")

    url = f"{base_url}/models"
    request_obj = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(request_obj, timeout=12) as response:
            payload = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return fail("密钥被拒绝了（401）。检查一下是不是少复制了字符，或者这个密钥已失效")
        return fail(f"端点返回 {exc.code}，可能地址不对")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return fail(f"连不上这个地址（{reason}）。检查网络，或者地址是否写成 https://")
    except Exception as exc:  # noqa: BLE001 —— 超时等异常也要变成人话
        return fail(f"测试失败：{exc}")

    models = []
    try:
        parsed = json_module.loads(payload)
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
            models = [str(one.get("id")) for one in parsed["data"] if isinstance(one, dict)]
    except (ValueError, TypeError):
        pass

    return ok({
        "status": status,
        "models": models[:40],
        "model": current.model,
        "model_found": current.model in models if models else None,
    })


@bp.delete("/llm/key")
def clear_key():
    """清掉密钥。

    单独一个动作是因为"我不想让它留在这台机器上"和"我要换一个密钥"
    在界面上是两个按钮；后者走 `PUT`，前者不该逼用户先删掉输入框里的内容再保存。
    """
    settings_mod.clear_llm_key()
    return ok(settings_mod.public_view())
