"""
AI 分析引擎。

调用 LLM 分析聊天记录，识别 bug 反馈，解析 JSON 输出。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, List

from astrbot.api import logger
from astrbot.api.star import Context

from .chat_buffer import MessageRecord


@dataclass
class BugItem:
    """单个 bug 分析结果。"""

    severity: str = "medium"  # low | medium | high | critical
    summary: str = ""
    analysis: str = ""
    related_messages: List[int] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """分析结果。"""

    result: str = "none"  # none | suspected | confirmed
    bugs: List[BugItem] = field(default_factory=list)
    raw_response: str = ""  # 原始 LLM 输出（用于调试和容错）
    error: str = ""  # 解析错误信息


class BugAnalyzer:
    """调用 LLM 分析聊天记录中的 bug 反馈。"""

    # 估算：每个字符约 1.5 tokens（中文更密，混合取平均）
    _TOKENS_PER_CHAR = 1.5
    # 保守的最大 prompt tokens（给大多数模型留余量）
    _MAX_PROMPT_TOKENS = 6000

    _SYSTEM_PROMPT = """\
你是一个专业的开源项目 Bug 分析助手。你正在监控一个 AstrBot（一个开源聊天机器人框架）及其插件的开发者交流群。

你的任务是分析群聊记录，识别其中是否包含对 AstrBot 或其插件的 bug 反馈、错误报告、功能缺陷描述或崩溃报告。

请严格按照以下 JSON 格式输出，不要添加任何 markdown 代码块标记，不要添加任何解释性文字：

{
    "result": "none" | "suspected" | "confirmed",
    "bugs": [
        {
            "severity": "low" | "medium" | "high" | "critical",
            "summary": "用一句话简要描述这个 bug",
            "analysis": "详细分析为什么判断这是 bug，基于哪些聊天记录得出此结论",
            "related_messages": [0, 1, 2]
        }
    ]
}

判断标准：
- "none"：聊天记录完全是闲聊、技术讨论、使用教程、功能咨询等，没有任何 bug 报告
- "suspected"：聊天记录中有人提到遇到问题、报错、异常现象，但信息不足以确定是软件 bug（可能是配置错误、用户误操作、环境问题等）
- "confirmed"：聊天记录中有明确的错误信息（如报错日志、堆栈跟踪、明确的异常行为描述），且多人确认或复现

严重程度标准：
- "critical"：导致程序崩溃、数据丢失、安全漏洞
- "high"：核心功能无法使用，严重影响用户体验
- "medium"：部分功能异常，有 workaround
- "low"：UI 显示问题、轻微的异常行为、文档错误

如果 result 为 "none"，bugs 数组必须为空 []。
如果 result 为 "suspected" 或 "confirmed"，bugs 数组必须至少包含一个 bug 对象。

注意：
- 不要对用户的正常技术讨论、功能请求、使用问题误判为 bug
- "我想要某个功能"属于功能请求，不是 bug
- "这个怎么用"属于使用问题，不是 bug
- "报错：xxx" "报错信息：xxx" 才是 bug 报告
"""

    def __init__(self, context: Context, config: dict):
        self.context = context
        self.provider_id = config.get("provider_id", "")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def analyze(
        self,
        messages: List[MessageRecord],
        umo: str,
    ) -> AnalysisResult:
        """分析消息列表，返回结构化结果。"""
        if not messages:
            return AnalysisResult(result="none")

        prompt = self._build_prompt(messages)
        prompt = self._truncate_if_needed(prompt)

        try:
            response = await self._call_llm(prompt, umo)
        except Exception as e:
            logger.error(f"[Analyzer] LLM 调用失败: {e}", exc_info=True)
            return AnalysisResult(error=str(e))

        response_text = getattr(response, "completion_text", "")
        if not response_text:
            logger.warning("[Analyzer] LLM 返回空文本")
            return AnalysisResult(raw_response="", error="empty response")

        return self._parse_response(response_text)

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    def _build_prompt(self, messages: List[MessageRecord]) -> str:
        """构建 User Prompt。"""
        lines = [
            f"以下是近期 {len(messages)} 条群聊记录（按时间顺序），"
            f"每条记录格式为 \"[索引] 时间 发送者: 内容\"：\n"
        ]
        for idx, msg in enumerate(messages):
            time_str = time.strftime(
                "%H:%M:%S", time.localtime(msg.timestamp)
            )
            lines.append(
                f"[{idx}] {time_str} {msg.sender_name}: {msg.content}"
            )
        lines.append("\n请分析这些记录中是否包含对 AstrBot 或其插件的 bug 反馈，输出 JSON 结果。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Token 截断
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """粗略估算 token 数。"""
        return int(len(text) * self._TOKENS_PER_CHAR)

    def _truncate_if_needed(self, prompt: str) -> str:
        """如果 prompt 过长，截断最早的消息，保留尾部指令。"""
        system_tok = self._estimate_tokens(self._SYSTEM_PROMPT)
        prompt_tok = self._estimate_tokens(prompt)
        total_tok = system_tok + prompt_tok

        if total_tok <= self._MAX_PROMPT_TOKENS:
            return prompt

        logger.warning(
            f"[Analyzer] Prompt 过长（估算 {total_tok} tokens），"
            f"开始截断早期消息"
        )

        # 分割为头部说明、消息行
        lines = prompt.split("\n")
        header = [l for l in lines if not l.startswith("[")]
        msg_lines = [l for l in lines if l.startswith("[")]

        # 从最早的消息开始删除，直到 token 足够
        while msg_lines and self._estimate_tokens(
            "\n".join(header + msg_lines)
        ) + system_tok > self._MAX_PROMPT_TOKENS:
            msg_lines.pop(0)

        truncated = "\n".join(header + msg_lines)
        logger.info(
            f"[Analyzer] 截断后剩余 {len(msg_lines)} 条消息，"
            f"估算 tokens: {self._estimate_tokens(truncated) + system_tok}"
        )
        return truncated

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str, umo: str) -> Any:
        """调用 LLM，返回 LLMResponse。"""
        provider_id = self.provider_id
        if not provider_id:
            prov = self.context.get_using_provider(umo)
            if prov:
                provider_id = prov.meta().id
            else:
                raise RuntimeError("未配置 Provider，且无法获取默认 Provider")

        logger.info(f"[Analyzer] 使用 Provider: {provider_id}")
        return await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=self._SYSTEM_PROMPT,
        )

    # ------------------------------------------------------------------
    # JSON 解析（多层容错）
    # ------------------------------------------------------------------

    def _parse_response(self, response_text: str) -> AnalysisResult:
        """解析 LLM 返回的 JSON 文本。"""
        result = AnalysisResult(raw_response=response_text)

        # 第 1 层：直接解析
        try:
            data = json.loads(response_text)
            return self._extract_result(data, response_text)
        except json.JSONDecodeError:
            pass

        # 第 2 层：从文本中提取 JSON 块（正则）
        try:
            json_text = self._extract_json_block(response_text)
            if json_text:
                data = json.loads(json_text)
                return self._extract_result(data, response_text)
        except json.JSONDecodeError:
            pass

        # 第 3 层：尝试修复常见 JSON 格式问题
        try:
            fixed = self._fix_json(response_text)
            data = json.loads(fixed)
            return self._extract_result(data, response_text)
        except (json.JSONDecodeError, ValueError):
            pass

        # 全部失败
        result.error = "JSON 解析失败"
        logger.warning(f"[Analyzer] 无法解析 LLM 输出: {response_text[:200]}...")
        return result

    def _extract_json_block(self, text: str) -> str | None:
        """从文本中提取 JSON 代码块或 JSON 对象。"""
        # 尝试匹配 markdown 代码块
        patterns = [
            r"```json\s*(.*?)\s*```",  # ```json ... ```
            r"```\s*(.*?)\s*```",       # ``` ... ```
            r"(\{[\s\S]*\})",           # 最外层 {...}
        ]
        for pat in patterns:
            match = re.search(pat, text, re.DOTALL)
            if match:
                extracted = match.group(1).strip()
                if extracted.startswith("{") or extracted.startswith("["):
                    return extracted
        return None

    def _fix_json(self, text: str) -> str:
        """尝试修复常见的 JSON 格式问题。"""
        # 去除 markdown 标记
        text = re.sub(r"```json\s*|\s*```", "", text)
        # 去除尾部逗号
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        return text.strip()

    def _extract_result(self, data: dict, raw: str) -> AnalysisResult:
        """从解析后的 dict 中提取结构化结果。"""
        result = AnalysisResult(raw_response=raw)

        result_str = data.get("result", "none")
        if result_str not in ("none", "suspected", "confirmed"):
            result.error = f"invalid result value: {result_str}"
            return result
        result.result = result_str

        if result_str == "none":
            return result

        bugs_data = data.get("bugs", [])
        if not isinstance(bugs_data, list):
            result.error = "bugs field is not a list"
            return result

        for bug_data in bugs_data:
            if not isinstance(bug_data, dict):
                continue
            bug = BugItem(
                severity=self._validate_severity(bug_data.get("severity", "medium")),
                summary=bug_data.get("summary", "") or "",
                analysis=bug_data.get("analysis", "") or "",
                related_messages=bug_data.get("related_messages", []) or [],
            )
            result.bugs.append(bug)

        return result

    @staticmethod
    def _validate_severity(value: Any) -> str:
        """验证 severity 值是否合法。"""
        valid = {"low", "medium", "high", "critical"}
        if isinstance(value, str) and value in valid:
            return value
        return "medium"
