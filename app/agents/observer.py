"""观察者：从学生这一轮的话里抽取学习信号。

为什么单独一个角色，而不是让讲师顺手判断：
    讲师此刻的注意力全在"这段内容怎么写"上；让它同时判断"学生是不是在抱怨"，
    两件事会互相干扰，而且讲师眼里只有这一轮，判断容易被自己的讲法带偏。
    观察者的任务窄得多，也因此更容易做好。

它在闭环里的位置是**入口**：
    signals.py 定契约 → 本模块（规则判不准时才问模型）→ profile_service 落库 → 画像

成本设计（重要）：
    绝大多数轮次**不该**走到这里。普通提问（"什么是逆矩阵"）在 signals 层就被判为
    "无信号"而直接返回，连一次调用都不会发生。只有"看着像反馈、但规则说不清"的消息
    才真的花钱问一次模型——这个判断由 signals.should_ask_model 负责把门。
"""

from __future__ import annotations

from typing import Any

from .. import signals
from .base import extract_json, load_prompt, run_json_once


def _parse_signals(raw: str) -> list[dict[str, Any]]:
    """解析模型的原始输出。

    这里的异常**是故意的**：
      - 根本不是 JSON、或者不是数组 → 抛异常，交给 run_json_once 重试；
      - 是数组但内容脏 → 由 parse_signals 过滤（丢掉非法 kind/label）；
      - 是空数组 → **正常返回**，不重试。

    最后一条很关键：契约里写着"宁可漏报，也不要瞎报"，
    "这一轮没有信号"是最常见的正确答案，不该被当成解析失败反复重试。
    """
    data = extract_json(raw)

    # 模型偶尔会包一层对象，这里容错（但不为它重试）
    if isinstance(data, dict) and isinstance(data.get("signals"), list):
        data = data["signals"]

    if data is None:
        raise ValueError("观察结果为空")
    if not isinstance(data, list):
        raise ValueError(f"观察结果不是数组：{type(data).__name__}")

    return signals.parse_signals(data)


def observe_turn(*, point_title: str, message: str) -> list[dict[str, Any]]:
    """观察学生这一轮说的话，返回学习信号列表（可能为空）。

    **这是对外唯一的入口**，它自带规则预筛，所以调用方不必、也不该自己去判断该不该调模型：

      ① 规则能直接判定的（"太难了""嗯""懂了"）→ 直接返回，**零模型成本**；
      ② 规则判不准、但看着像反馈的（"这个推导有点绕"）→ 才真的调用一次模型；
      ③ 普通提问（"什么是逆矩阵"）→ 直接返回空列表，**零模型成本**。

    把预筛放在入口而不是交给调用方，是因为"忘了先判断"的代价是每一轮都多付一次调用——
    这种错误不会报错，只会悄悄把 API 账单翻倍。
    """
    text = (message or "").strip()
    if not text:
        return []

    offline = signals.offline_signals(text)
    if offline:
        return offline

    if not signals.should_ask_model(text):
        return []

    return _ask_model(point_title=point_title, message=text)


def _ask_model(*, point_title: str, message: str) -> list[dict[str, Any]]:
    """真的去问一次模型（只由 observe_turn 在预筛通过后调用）。"""
    description = load_prompt("observer").format(
        point_title=point_title or "（未指定）",
        message=message,
    )

    return run_json_once(
        role="学习观察者",
        goal="判断学生这一轮是否流露出需要调整教学的状态",
        backstory=(
            "你只做一件事：听学生怎么说话。"
            "你不讲课、不答疑、不写内容，也不负责判断课程讲到哪了。"
            "你很清楚自己拿不准的时候应当保持沉默——"
            "因为你的判断会直接改变系统对这个学生的看法，"
            "误报一次，后面每一轮都会跟着错，而学生根本不知道为什么。"
        ),
        description=description,
        expected_output="一个 JSON 数组，元素含 kind / label / evidence / strength",
        parser=_parse_signals,
        temperature=0.1,  # 观察要稳定，不要发挥
    )
