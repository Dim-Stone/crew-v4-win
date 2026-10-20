"""智能体层。

目前的角色：
    ① 摸底调查官 interviewer     —— 用户情况调查 → 画像
    ② 学习规划师 planner         —— 画像 + 方向 → 学习大纲
    ③ 备课教师   lesson_planner  —— 单个知识点 → 教案（分几步、每步要什么）
    ④ 授课讲师   lecturer        —— 逐知识点讲授 / 答疑（流式，沿教案走）
    ⑤ 图解生成师 visual          —— 知识点 → Mermaid 图
    ⑥ 学习观察者 observer        —— 学生这一轮的话 → 学习信号（"反馈—自适应"闭环的入口）

③ 与 ② 的分工：② 管"学科怎么拆"，粒度是一门课；③ 管"这一个点怎么讲"，粒度是一节课。
两者粒度不同，混在同一个提示词里会互相打架，所以分开。

⑥ 与 ④ 的分工：④ 的注意力在"这段内容怎么写"，⑥ 只问"学生现在什么状态"。
   让同一个角色既讲课又揣摩学生的情绪，两件事都会做不好；
   而且 ④ 只见这一轮，容易被自己的讲法带偏。

立项书里的「出题测评官」按一期范围暂缓，
接口位置预留在 services 层（见 services/__init__.py 的说明）。
"""

from . import base, interviewer, lesson_planner, lecturer, observer, planner, visual

__all__ = [
    "base",
    "interviewer",
    "lesson_planner",
    "lecturer",
    "observer",
    "planner",
    "visual",
]
