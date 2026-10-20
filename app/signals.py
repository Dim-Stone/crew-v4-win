"""学习信号契约：把"学习者在对话里流露的反馈"变成结构化、可判定的东西。

这是"反馈—自适应"闭环的第一环。整条链子的走向是：

    对话 ──► 学习信号 ──► ProfileEvent 流水 ──► 画像字段 ──► 下一轮的讲法
             ↑ 本模块定义        ↑ profile_service        ↑ prompts/lecturer.md

为什么单独做成一个零依赖模块：
    - **抽取端**（agents/observer.py）与**消费端**（services/profile_service.py）必须用同一套
      定义，否则迟早出现"两处标准打架"——一边说"太难"算强信号，另一边不认。
    - 本模块不 import 任何本项目模块（更不 import crewai），任何层都能安全引用：
      既不会引入循环依赖，也不会把 crewai 那 1.6 秒的导入代价带进无关路径。

━━━ 两类信号（按可信度从高到低）━━━

① explicit 显式抱怨 —— "太难了""听不懂""讲太快了"
     可信度最高：学生主动说的。常见说法**规则直接就能判**，不必调模型（省钱）。
     但自然语言千变万化（"这块儿能不能拆细一点""我卡住了"），判不准时才交给模型。

② passive 隐式冷淡 —— "嗯""哦""随便"，或极短的回复
     可信度低：可能只是懒得打字，也可能是真的失去兴趣，两者从一句话里分不出来。
     **只记流水，不改画像**——单凭一条"嗯"就改画像，那是噪声不是信号。

━━━ 为什么没有"行为停滞"这一类（曾经有，已删）━━━

最初的契约里还有第三类 `stalled`（同一个知识点停留太久 = 卡住了），
想用它捕捉"学生一声不吭地卡住"——那确实是 `passive` 覆盖不到的情况。

但接数据源时发现它**在当前架构里不可能成立**：
唯一可用的观测值 `lesson_plans.step_rounds` 会被 `lesson_service.MAX_ROUNDS_PER_STEP`
（讲满 3 轮就强制推进）**重置为 0**，也就是说它永远到不了 3；
阈值定 2 又会每步都触发，只是往流水里灌噪音。

这类问题不该靠"把阈值调小"来假装解决。所以这个类别连同 `stalled_signal()`
一起删掉了，等有了真正独立的观测源（例如按知识点统计已讲轮数）再加回来——
**契约里宁可少一类，也不要留一个永远不会触发的判断**。

━━━ 信号的数据形状 ━━━

    {
      "kind":     "explicit" | "passive",
      "label":    "too_hard" | "too_fast" | "disengaged" | "understood" | "wants_more",
      "evidence": "学生原话片段",
      "strength": 1 | 2 | 3,
    }

**strength 就是"要不要改画像"的那条线**：
    只有 `strength >= STRONG_THRESHOLD` 的信号才允许改画像字段，低于它的只写 ProfileEvent 流水。
    这条规则存在的唯一理由是**防震荡**——画像每轮抖动，系统行为就会前后矛盾，
    那比"不个性化"更糟：学生只会觉得这个老师记性不好。
"""

from __future__ import annotations

# ---------------- 信号类别 ----------------

KIND_EXPLICIT = "explicit"   # 学生主动说的抱怨/评价
KIND_PASSIVE = "passive"     # 敷衍、极短回复

KINDS = (KIND_EXPLICIT, KIND_PASSIVE)

# ---------------- 信号标签 ----------------

LABEL_TOO_HARD = "too_hard"        # 太难，懂不了
LABEL_TOO_FAST = "too_fast"        # 节奏太快，跟不上
LABEL_DISENGAGED = "disengaged"    # 冷淡、想放弃
LABEL_UNDERSTOOD = "understood"    # 正反馈：听懂了
LABEL_WANTS_MORE = "wants_more"    # 想更深、想多看例子

VALID_LABELS = frozenset({
    LABEL_TOO_HARD,
    LABEL_TOO_FAST,
    LABEL_DISENGAGED,
    LABEL_UNDERSTOOD,
    LABEL_WANTS_MORE,
})

# ---------------- 强度 ----------------

STRENGTH_WEAK = 1
STRENGTH_MEDIUM = 2
STRENGTH_STRONG = 3

MIN_STRENGTH = STRENGTH_WEAK
MAX_STRENGTH = STRENGTH_STRONG

# 只有达到这个强度的信号才允许改画像字段；其余只写流水。
STRONG_THRESHOLD = STRENGTH_STRONG

# ---------------- 规则层（能判就不调模型）----------------

# 高置信规则：命中就直接产出信号，零模型成本。
# 命中优先级按列表顺序，一个消息可以命中多条（例如"太快了，听不懂"）。
_STRONG_RULES: tuple[tuple[tuple[str, ...], str, int], ...] = (
    (
        ("太难", "听不懂", "听不明白", "看不明白", "没听懂", "没听明白",
         "不明白", "没明白", "不太明白", "不太懂", "没太懂",
         "不理解", "看不懂", "不会做", "跟不上思路"),
        LABEL_TOO_HARD,
        STRENGTH_STRONG,
    ),
    (
        ("太快", "跟不上", "讲慢", "慢点", "再慢", "缓一缓", "来不及"),
        LABEL_TOO_FAST,
        STRENGTH_STRONG,
    ),
    (
        ("算了", "不想学", "放弃", "没意思", "无聊", "学不会"),
        LABEL_DISENGAGED,
        STRENGTH_STRONG,
    ),
    (
        ("懂了", "明白了", "清楚了", "会了", "理解了"),
        LABEL_UNDERSTOOD,
        STRENGTH_MEDIUM,
    ),
    (
        ("再来一个", "多讲点", "再深", "更深", "更多例子", "多举", "拓展"),
        LABEL_WANTS_MORE,
        STRENGTH_MEDIUM,
    ),
)

# 判不准、但"看着像反馈"的模糊说法——这些才值得花一次模型调用。
# 注意这里刻意不放"太难""听不懂"这类：它们已经被规则稳稳接住了。
#
# "难"和"不懂"单独放进来是必要的：学生更常说"这有点难""我不太懂"，
# 而这两个说法都不含上面那些完整词组。漏掉它们，这类反馈就完全消失了
# （不会误改画像，但会让学生觉得"说了也白说"——比误报更伤）。
_VAGUE_HINTS = ("绕", "复杂", "乱", "晕", "卡", "吃力", "费劲", "勉强", "有点懵",
                "难", "不懂")

# 敷衍的极短回复（规则可判，不需要模型）
_SHORT_REPLIES = frozenset({
    "嗯", "哦", "好", "好的", "行", "可以", "随便", "都行", "嗯嗯", "哦哦", "。", "…",
})
SHORT_REPLY_MAX_LEN = 4


def _text_of(message: str | None) -> str:
    return (message or "").strip()


def offline_signals(message: str) -> list[dict]:
    """规则能高置信判定的信号。

    返回空列表**不代表"没有信号"**，只代表"规则没把握"——
    调用方可以接着用 `should_ask_model()` 决定要不要问模型。
    """
    text = _text_of(message)
    if not text:
        return []

    signals: list[dict] = []
    for patterns, label, strength in _STRONG_RULES:
        hit = next((p for p in patterns if p in text), None)
        if hit is not None:
            signals.append({
                "kind": KIND_EXPLICIT,
                "label": label,
                "evidence": hit,
                "strength": strength,
            })

    if not signals and is_short_reply(text):
        signals.append({
            "kind": KIND_PASSIVE,
            "label": LABEL_DISENGAGED,
            "evidence": text,
            "strength": STRENGTH_WEAK,
        })

    return signals


def is_short_reply(message: str) -> bool:
    """是不是敷衍式的极短回复（"嗯""哦""好"）。"""
    text = _text_of(message)
    return bool(text) and len(text) <= SHORT_REPLY_MAX_LEN and text in _SHORT_REPLIES


def should_ask_model(message: str) -> bool:
    """规则判不准、但看着像反馈——值得花一次模型调用去看清楚。

    这层存在的意义就是**省钱**：普通提问（"什么是逆矩阵"）在这里返回 False，
    整轮不会多付任何一次调用；只有确实像在表达感受的消息才会进模型。
    """
    text = _text_of(message)
    if not text:
        return False
    if offline_signals(text):
        return False  # 规则已经判出来了，不用再问
    return any(hint in text for hint in _VAGUE_HINTS)


def parse_signals(payload: object) -> list[dict]:
    """把模型返回的 JSON（形状不可信）校验成合法信号列表。

    模型输出永远当**不可信输入**处理：丢掉不认识的 kind / label、
    把 strength 夹到 1..3、丢掉空 evidence。
    宁可丢掉一个信号，也不要让脏数据顺着闭环流进画像——
    画像一旦被脏数据改写，后面每一轮都会被它带偏。
    """
    if not isinstance(payload, list):
        return []

    cleaned: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        label = str(item.get("label") or "").strip()
        if kind not in KINDS or label not in VALID_LABELS:
            continue
        evidence = str(item.get("evidence") or "").strip()
        if not evidence:
            continue
        try:
            strength = int(item.get("strength"))
        except (TypeError, ValueError):
            continue
        cleaned.append({
            "kind": kind,
            "label": label,
            "evidence": evidence[:200],
            "strength": max(MIN_STRENGTH, min(MAX_STRENGTH, strength)),
        })
    return cleaned
