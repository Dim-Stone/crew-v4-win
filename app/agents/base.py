"""智能体公共装配。

设计要点：
1. **不做 inputs 模板插值**——提示词在 Python 里拼成完整字符串再交给 Crew，
   避免任务描述里出现花括号导致 CrewAI 插值报错。
2. 每个智能体 = 一个单任务 Crew。一期不需要层级协作，
   协作通过共享的画像与大纲在大纲/回合之间传递（见 services 层）。
3. 密钥没配置时抛 LLMNotConfigured，由 API 层转成用户看得懂的中文提示。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from crewai import LLM, Agent, Crew, Process, Task

from ..config import Config

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class LLMNotConfigured(RuntimeError):
    """没有配置大模型密钥。"""


def load_prompt(name: str) -> str:
    """读取 prompts/<name>.md 提示词模板。"""
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"缺少提示词文件: {path}")
    return path.read_text(encoding="utf-8")


def build_llm(*, stream: bool = False, temperature: float | None = None) -> LLM:
    if not Config.llm_ready():
        raise LLMNotConfigured(
            "还没有配置大模型密钥。请把 .env.example 复制成 .env，"
            "填入 LLM_API_KEY（DeepSeek 密钥）后重启服务。"
        )
    return LLM(
        model=Config.LLM_MODEL,
        api_key=Config.LLM_API_KEY,
        base_url=Config.LLM_BASE_URL,
        temperature=Config.LLM_TEMPERATURE if temperature is None else temperature,
        stream=stream,
        # 上游"连接不断、数据不来"时，没有超时就会永远停在"讲课中"。
        # 给一个上界：至少能明确失败、能重试，而不是让界面一直转圈。
        timeout=Config.LLM_TIMEOUT,
    )


def _make_crew(role: str, goal: str, backstory: str, description: str,
               expected_output: str, *, stream: bool = False,
               temperature: float | None = None) -> Crew:
    agent = Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        llm=build_llm(stream=stream, temperature=temperature),
        allow_delegation=False,
        verbose=False,
        respect_context_window=True,
    )
    task = Task(
        description=description,
        expected_output=expected_output,
        agent=agent,
    )
    return Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        stream=stream,
    )


def run_once(role: str, goal: str, backstory: str, description: str,
             expected_output: str, *, temperature: float | None = None) -> str:
    """执行一个单任务 Crew，返回纯文本产出。"""
    crew = _make_crew(role, goal, backstory, description, expected_output,
                      stream=False, temperature=temperature)
    output = crew.kickoff()
    return getattr(output, "raw", str(output))


def stream_once(role: str, goal: str, backstory: str, description: str,
                expected_output: str, *, temperature: float | None = None) -> Iterator[str]:
    """流式执行，逐段 yield 文本增量。用于教学回合并经 SSE 推给前端。"""
    crew = _make_crew(role, goal, backstory, description, expected_output,
                      stream=True, temperature=temperature)
    streaming = crew.kickoff()
    for chunk in streaming:
        content = getattr(chunk, "content", "") or ""
        if content:
            yield content


def run_json_once(
    role: str,
    goal: str,
    backstory: str,
    description: str,
    expected_output: str,
    parser: Callable[[str], Any],
    *,
    temperature: float | None = None,
    retries: int = 2,
) -> Any:
    """执行任务并解析 JSON，失败自动重试。

    为什么需要重试：模型偶尔会不按格式输出——加一句前言、把 JSON 包在别的东西里、
    或者干脆换个结构。这类失败是**随机**的（实测同一个提示词，第一次失败、第二次就正常），
    重试一两次几乎总能拿到合规输出。
    这比把提示词写得更长更可靠：提示词再严也挡不住偶发情况。
    """
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        prompt = description
        if attempt > 0:
            prompt += (
                "\n\n【重试提醒】上一次的回答无法解析成要求的 JSON。"
                "这一次请只输出那个 JSON 对象本身："
                "不要开场白、不要结束语、不要代码围栏、不要用 markdown 表格代替 JSON。"
            )

        raw = run_once(
            role=role,
            goal=goal,
            backstory=backstory,
            description=prompt,
            expected_output=expected_output,
            temperature=temperature,
        )
        try:
            return parser(raw)
        except ValueError as exc:
            last_error = exc

    assert last_error is not None
    raise last_error


# --------------------------------------------------------------------------
# JSON 解析
# --------------------------------------------------------------------------

def extract_json(text: str) -> Any:
    """从模型输出里稳健地抠出 JSON。

    依次尝试：整体解析 → 去掉 ```json 围栏 → 扫描第一个平衡的 {} / []。
    模型偶尔会在 JSON 前后加解释性文字，这里都兜住。
    """
    if not text or not text.strip():
        raise ValueError("模型返回为空，无法解析出 JSON")

    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if fence:
        s = fence.group(1).strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(s[i:])
                return obj
            except json.JSONDecodeError:
                continue

    raise ValueError("模型输出里找不到可解析的 JSON，原始内容前 200 字：" + text[:200])
