"""服务层。

分层纪律：
    api/       只做参数校验与响应组装
    services/  只做业务编排（不写 SQL 细节、不写提示词）
    agents/    只负责智能体
    prompts/   文案不写进 .py

一期功能边界（与基础方案.md 的"一期范围"一致）：
    ✅ 摸底调查 → 画像
    ✅ 生成学习大纲（可人工修改前的只读版本）
    ✅ 逐知识点授课 + 答疑（流式长对话）
    ✅ Mermaid 图解（可选按钮）
    ✅ 卡片课堂（`page_service`：把讲授切成页，一步一页）
    ✅ 出题测评官（`agents/quizmaster.py` + `quiz_service`：选择 / 多选 /
       填空 / 判断，规则判分，结果以 `quiz_result` 信号进 observer）
"""

from . import (
    curriculum_service,
    lecture_notes,
    lesson_service,
    page_service,
    profile_service,
    quiz_service,
    runtime,
    teaching_service,
    todo_service,
)

__all__ = [
    "curriculum_service",
    "lecture_notes",
    "lesson_service",
    "page_service",
    "profile_service",
    "quiz_service",
    "runtime",
    "teaching_service",
    "todo_service",
]
