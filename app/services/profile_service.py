"""画像服务：读取、写入、摘要。

画像是"自适应"的输入源头：
    摸底问卷 → parse_intake → 此处落库 → 每轮教学拼进提示词 → 影响讲法。
"""

from __future__ import annotations

from ..extensions import db
from ..models import LearnerProfile, ProfileEvent
from ..signals import (
    LABEL_DISENGAGED,
    LABEL_TOO_FAST,
    LABEL_TOO_HARD,
    LABEL_UNDERSTOOD,
    LABEL_WANTS_MORE,
    STRONG_THRESHOLD,
)


def get_profile() -> LearnerProfile:
    """单用户模式：画像固定为 id=1，没有就建一个空的。"""
    profile = db.session.get(LearnerProfile, 1)
    if profile is None:
        profile = LearnerProfile(id=1)
        db.session.add(profile)
        db.session.commit()
    return profile


def update_from_intake(answers: dict[str, str]) -> LearnerProfile:
    """把摸底回答解析成画像并写库。"""
    from ..agents.interviewer import parse_intake

    direction = answers.get("_direction", "")
    parsed = parse_intake(direction, answers)

    profile = get_profile()
    profile.ability_level = parsed["ability_level"]
    profile.interests = parsed["interests"]
    profile.purpose = parsed["purpose"]
    profile.style_preference = parsed["style_preference"]
    profile.time_budget = parsed["time_budget"]
    profile.summary_md = parsed["summary_md"]
    profile.raw_intake = parsed["raw_intake"]
    db.session.commit()
    return profile


def summary_text() -> str:
    """给提示词用的画像摘要（三段拼起来）。

    ① 摸底摘要  静态：学习者是谁（摸底那一次得到，之后不变）
    ② 讲法节奏  动态：系统依据近期反馈调整出的讲法（闭环的**输出**）
    ③ 最近反馈  动态：学生刚刚流露了什么（闭环的**回程**）

    ②③ 才是"反馈—自适应"真正起作用的地方。
    没有它们，画像就只是摸底那一次的快照，之后永远不变——那正是本项目原先的状态。
    """
    profile = get_profile()
    lines: list[str] = []

    if profile.is_filled:
        if profile.summary_md:
            lines.append(profile.summary_md)
        # 摘要缺失时用结构化字段兜底
        else:
            if profile.ability_level != "unknown":
                lines.append(f"- 基础水平：{profile.ability_level}")
            if profile.purpose:
                lines.append(f"- 学习目的：{profile.purpose}")
            if profile.style_preference:
                lines.append(f"- 讲法偏好：{profile.style_preference}")

    # 即使还没做过摸底，只要已经有反馈信号，也要让讲师看见
    pace = pace_text()
    if pace:
        lines.append(f"- 讲法节奏（系统根据学生反馈调整）：{pace}")

    recent = recent_signal_lines()
    if recent:
        lines.append("- 学生最近的反馈：" + "；".join(recent))

    return "\n".join(lines)


def add_event(turn_id: int | None, signals: dict, note: str = "") -> None:
    """记录一条画像演化流水。

    这张表是"系统为什么这样调整"的凭证：观测到的信号、画像变更、以及
    展示给学生的说明文案，都从这里读（见 adaptation_note / recent_signal_lines）。
    所以它记的不只是结果，还要记**原因**。
    """
    import json

    db.session.add(ProfileEvent(
        turn_id=turn_id,
        signals=json.dumps(signals, ensure_ascii=False),
        note=note,
    ))
    db.session.commit()


# ---------------- 反馈闭环：信号 → 流水 → 画像 ----------------

# 讲法节奏的阶梯（从快到慢）。**只能沿阶梯挪一格**，不允许跳档。
_PACE_LADDER = ("faster", "normal", "slower")
_PACE_DEFAULT = "normal"

# 哪些信号会把节奏推向"更慢"。
#
# 只做单向（只会变慢）是**刻意的**：放宽容易、收紧难。
# 让学生觉得"老师突然变快、我跟不上了"的代价，
# 远大于"老师讲得比我能接受的细了一点"。
# 加速方向留给后续版本——那需要更可靠的证据（例如连续多轮无困惑 + 主动要求深入）。
_SLOW_DOWN_LABELS = frozenset({LABEL_TOO_HARD, LABEL_TOO_FAST})


def strong_signals(signals: list[dict]) -> list[dict]:
    """筛出强度达到阈值、**允许改画像**的信号。

    低于阈值的信号不是被丢掉——它们仍会写进 ProfileEvent 流水，只是不参与改画像。
    理由就是防震荡：一条"嗯"就调画像，系统行为会前后矛盾，
    而学生只会觉得"这个老师记性不好"，那比"不个性化"更糟。
    """
    return [s for s in signals if int(s.get("strength", 0)) >= STRONG_THRESHOLD]


def record_signals(*, turn_id: int | None, signals: list[dict], point_title: str = "") -> None:
    """把这一轮观测到的信号写进 ProfileEvent 流水。

    注意记的是**全部信号**，包括那些强度不足以改画像的弱信号——
    它们是"为什么画像没变"的证据，比画像本身更值得留在研究数据里。
    """
    if not signals:
        return
    add_event(
        turn_id,
        {"point": point_title, "signals": signals},
        note=f"观测到 {len(signals)} 条学习信号",
    )


def observe_turn(*, turn_id: int | None, point_title: str, user_message: str) -> list[dict]:
    """闭环第二环：观察这一轮 → 记录流水 → 按阈值渐进更新画像。

    顺序是**先记流水、再改画像**，这一点很重要：
    即使随后改画像出了任何问题，证据也已经留在库里了——
    画像是"结论"，流水是"得出这个结论的依据"，依据不该因为结论出错而一起丢掉。

    返回观测到的信号列表（可能为空）。
    """
    from ..agents.observer import observe_turn as observe

    found = observe(point_title=point_title, message=user_message)
    if not found:
        return []

    record_signals(turn_id=turn_id, signals=found, point_title=point_title)

    strong = strong_signals(found)
    if strong:
        profile = get_profile()
        changes = apply_signals(profile, strong)
        if changes:
            db.session.commit()
            # 改动本身也留一条流水，这样"画像为什么变了"永远查得到
            add_event(turn_id, {"changes": changes}, note="画像已更新")

    return found


def apply_signals(profile: LearnerProfile, signals: list[dict]) -> list[str]:
    """把强信号**渐进**地反映到画像字段上，返回实际发生的改动说明。

    "渐进"由两条硬约束保证：
      ① 同一轮里同一方向最多挪一档——多个信号叠加也不许跳档；
      ② 只朝变慢一个方向（见 _SLOW_DOWN_LABELS 的说明）。
    """
    labels = {s["label"] for s in signals}
    changes: list[str] = []

    if labels & _SLOW_DOWN_LABELS:
        current = profile.pace or _PACE_DEFAULT
        shifted = _shift_pace(current, +1)
        if shifted != current:
            changes.append(f"讲法节奏 {current} → {shifted}")
            profile.pace = shifted

    return changes


def _shift_pace(current: str | None, direction: int) -> str:
    """沿节奏阶梯挪一档（+1 更慢、-1 更快），越界就停在端点。"""
    pace = current if current in _PACE_LADDER else _PACE_DEFAULT
    index = _PACE_LADDER.index(pace) + direction
    index = max(0, min(len(_PACE_LADDER) - 1, index))
    return _PACE_LADDER[index]


# ---------------- 反馈闭环的回程：信号 → 下一轮提示词 ----------------

# 放进提示词的"最近反馈"条数。
# 不给全部历史——那是上下文污染；只给最近几条，让讲师看得见"学生刚刚怎么了"就够了。
RECENT_SIGNAL_LIMIT = 3

# 信号标签 → 给讲师看的人话
_LABEL_TEXT = {
    LABEL_TOO_HARD: "觉得太难、听不懂",
    LABEL_TOO_FAST: "觉得太快、跟不上",
    LABEL_DISENGAGED: "反应冷淡（可能没兴趣或只是敷衍）",
    LABEL_UNDERSTOOD: "表示听懂了",
    LABEL_WANTS_MORE: "希望讲得更深、多看例子",
}

# 节奏枚举 → 给讲师的明确指令（要说"怎么做"，而不是只给一个词）
_PACE_TEXT = {
    "slower": "比默认更慢更细：多铺垫、多举具体例子，公式出现前先把直觉讲清",
    "faster": "可以比默认快一些：少铺垫，把步子迈在推导和易错点上",
}


def _signals_in(event: ProfileEvent) -> list[dict]:
    """从一条流水里取出信号列表（JSON 可能损坏，当作没有）。"""
    import json

    try:
        data = json.loads(event.signals or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("signals")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def recent_signal_lines(limit: int = RECENT_SIGNAL_LIMIT) -> list[str]:
    """给**讲师**看的近期反馈（人话）。"""
    return [text for text in (_LABEL_TEXT.get(label) for label in _recent_labels(limit)) if text][:limit]


def _recent_labels(limit: int) -> list[str]:
    """最近若干条信号的 label（去重、按时间倒序）。

    讲师视角与学生视角只差一张"翻译表"，取数逻辑共用这一处——
    否则两份实现迟早会在"去重规则""取几条"上悄悄走偏。

    **这里刻意不按 limit 截断**：翻译是调用方的事，而库里的 label 未必都认得
    （例如早期版本留下的标签）。如果在这里就截断，那些翻译不出来的标签会白占名额，
    调用方过滤之后反而凑不够条数——这个坑是测试抓出来的，别改回去。
    """
    events = (
        ProfileEvent.query
        .order_by(ProfileEvent.id.desc())
        .limit(limit * 3)  # 有些流水只记画像改动、没有信号，需要跳过
        .all()
    )

    labels: list[str] = []
    for event in events:
        for signal in _signals_in(event):
            label = signal.get("label")
            if label and label not in labels:  # 同一件事连说三遍没有信息量
                labels.append(label)
    return labels


def pace_text() -> str:
    """把 pace 枚举翻成给讲师的讲法指令；normal/未知返回空串。"""
    return _PACE_TEXT.get(get_profile().pace or "", "")


# ---------------- 给学生看的"系统正在怎么适应你" ----------------

# 界面上那句提示里列几条原因。只给 1～2 条：那是给学生看的，不是日志。
STUDENT_REASON_LIMIT = 2

# 学生视角的说法。与 _LABEL_TEXT 同源，但那是讲师视角（"觉得太难、听不懂"），
# 这里是学生视角（"太难、听不懂"）——人称不同，不能直接复用。
_SIGNAL_STUDENT_TEXT = {
    LABEL_TOO_HARD: "太难、听不懂",
    LABEL_TOO_FAST: "讲得太快",
    LABEL_DISENGAGED: "有点提不起劲",
    LABEL_UNDERSTOOD: "听懂了",
    LABEL_WANTS_MORE: "想再深入一点",
}

_PACE_STUDENT_TEXT = {
    "slower": "已放慢讲解节奏：多铺垫、多举例子",
    "faster": "已加快讲解节奏：少铺垫、直接讲要点",
}


def adaptation_note() -> str:
    """一句给**学生**看的"系统正在怎么适应你"；没发生过调整时返回空串。

    为什么必须让学生看见：自适应只体现在讲法里的话，学生并不知道系统在听，
    只会把"老师这次讲得慢"当成偶然。让他看见"这是根据你说过的话调整的"，
    反馈才真正形成回路——他才愿意继续说。

    没有调整时返回**空串**而不是"暂无调整"：后者只是噪音，
    还额外传递了"系统什么都没做"的暗示。
    """
    pace = _PACE_STUDENT_TEXT.get(get_profile().pace or "")
    if not pace:
        return ""

    reasons = [
        text
        for text in (_SIGNAL_STUDENT_TEXT.get(label) for label in _recent_labels(STUDENT_REASON_LIMIT))
        if text
    ][:STUDENT_REASON_LIMIT]
    if reasons:
        return f"{pace}（因为你说过：{'、'.join(reasons)}）"
    return pace


def to_dict(profile: LearnerProfile) -> dict:
    """画像的结构化视图（给接口用）。

    注意 `pace` 与摸底问卷里那个同名字段**不是一回事**：
    问卷里的 `pace` 是学生**自述**的偏好（自由文本，最终落进 `style_preference`）；
    这里的 `pace` 是系统**观测**学生反馈之后调整出来的枚举（faster / normal / slower）。
    两者同名只是巧合，前端取值时别混。
    """
    return {
        "ability_level": profile.ability_level,
        "interests": profile.interests,
        "purpose": profile.purpose,
        "style_preference": profile.style_preference,
        "pace": profile.pace or "normal",
        # 给界面直接用的一句话（见 adaptation_note）。
        # 放在这里是为了**刷新页面后也能显示**：进度快照只在授课回合里推，
        # 而学生一刷新就没有快照了。
        "adaptation": adaptation_note(),
        "time_budget": profile.time_budget,
        "summary_md": profile.summary_md,
        "is_filled": profile.is_filled,
    }
