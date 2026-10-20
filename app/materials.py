"""资料模式的契约（零依赖）。

为什么单独一个模块，而不是放进 `services/materials_service.py`：
    `agents/` 也要用这几个常量，而 `agents/` 不该 import `services/`
    （那会绕一大圈，还会把服务层的依赖拉进智能体装配）。
    这与 `app/signals.py` 是同一个理由。

两种"依据强度"的存在理由：
    用户手上的资料**常常只覆盖一部分**——几份讲义、一份课件、或者只挑了重点。
    这时"严格照资料讲"会让课程缺掉一半；而"资料只当参考"又可能让模型
    把自己的知识混进来、还说不清哪些是自己加的。两种取舍各有代价，
    所以让用户自己选，而不是替他决定。
"""

from __future__ import annotations

# 严格：资料就是大纲。不得超出、不得补充，每个知识点都要有出处。
MODE_STRICT = "strict"
# 参考：资料是最重要的输入之一，允许按学科需要补全；补充的内容 sources 留空。
MODE_REFERENCE = "reference"

# 不使用资料模式：沿用原有流程（模型自由编排）。
# 它**不放进 MODES**——那两个是"依据强度"，这一个表示"压根不用资料"，
# 是一类不同的东西。混在一起会让 normalize_mode 把它当成合法的依据强度。
MODE_NONE = ""

MODES = (MODE_STRICT, MODE_REFERENCE)

MODE_LABELS = {
    MODE_STRICT: "严格按资料讲",
    MODE_REFERENCE: "资料作参考",
}


def normalize_mode(value: str | None) -> str:
    """把外部传来的模式收敛到已知值。

    不认识的值一律退回**参考模式**——这是更宽容的一侧：
    编排出问题最多是多讲了几点、并且都标明了出处；
    而误判成"严格"会让用户的课程凭空缺掉一大块，且他不会立刻发现。
    """
    text = (value or "").strip().lower()
    return text if text in MODES else MODE_REFERENCE


def uses_materials(value: str | None) -> bool:
    """这门课是否走资料模式。

    注意与 `normalize_mode` 的分工：那个是"用资料时按哪种强度"，
    这个是"到底用不用资料"。两者不能互相替代——
    没有资料的课程**不该**被 normalize_mode 塞成 reference。
    """
    return (value or "").strip().lower() in MODES
