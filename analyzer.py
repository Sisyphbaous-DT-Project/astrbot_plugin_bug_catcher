"""
AI 分析引擎。

调用 LLM 分析聊天记录，识别 bug 反馈，解析 JSON 输出。
支持：图片/视频清洗、已有 bug 去重、群信息溯源、一次分析识别多条 bug。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from astrbot.api import logger
from astrbot.api.star import Context

from .chat_buffer import MessageRecord


# 图片 URL 正则（含常见扩展名和查询参数）
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s<>\"{}|\\^`\[\]]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg)(?:\?[^\s]*)?",
    re.IGNORECASE,
)
# 视频 URL 正则
_VIDEO_URL_RE = re.compile(
    r"https?://[^\s<>\"{}|\\^`\[\]]+\.(?:mp4|avi|mov|webm|flv|mkv)(?:\?[^\s]*)?",
    re.IGNORECASE,
)
# 用于判断清洗后是否只剩占位符
_EMPTY_AFTER_SANITIZE_RE = re.compile(r"^(\[(?:图片|视频)\]\s*)+$")
_LINE_BREAK_RE = re.compile(r"[\r\n]+")


def _as_text(value: Any, default: str = "") -> str:
    """将模型/事件传入的任意值安全转为字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _one_line_text(value: Any, default: str = "") -> str:
    """转为单行文本，避免 Prompt/列表展示被换行注入破坏结构。"""
    return _LINE_BREAK_RE.sub(" ", _as_text(value, default)).strip()


def _multiline_text(value: Any, default: str = "") -> str:
    """转为多行文本，仅规范 CRLF/CR，保留分析内容中的换行。"""
    return _as_text(value, default).replace("\r\n", "\n").replace("\r", "\n")


def _bool_value(value: Any) -> bool:
    """解析模型返回的布尔字段，避免字符串 'false' 被 bool() 误判为 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


@dataclass
class BugItem:
    """单个 bug 分析结果。"""

    severity: str = "medium"  # low | medium | high | critical
    summary: str = ""
    analysis: str = ""
    related_messages: List[int] = field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of_id: str = ""
    primary_message_index: int = -1  # 导致判断为 bug 的最关键消息索引，-1 表示未指定


@dataclass
class AnalysisResult:
    """分析结果。"""

    result: str = "none"  # none | suspected | confirmed
    bugs: List[BugItem] = field(default_factory=list)
    raw_response: str = ""  # 原始 LLM 输出（用于调试和容错）
    error: str = ""  # 解析错误信息


class BugAnalyzer:
    """调用 LLM 分析聊天记录中的 bug 反馈。"""

    _SYSTEM_PROMPT = """\
你是一个专业的开源项目 Bug 分析助手。你正在监控一个 AstrBot（一个开源聊天机器人框架）及其插件的开发者交流群。

你的任务是分析群聊记录，识别其中是否包含对 AstrBot 或其插件的 bug 反馈、错误报告、功能缺陷描述或崩溃报告。

请严格按照以下 JSON 格式输出，不要添加任何 markdown 代码块标记，不要添加任何解释性文字：

{
    "result": "confirmed",
    "bugs": [
        {
            "severity": "high",
            "summary": "用一句话简要描述这个 bug",
            "analysis": "详细分析为什么判断这是 bug，基于哪些聊天记录得出此结论。如果是重复 bug，请说明首次发现时间和之前的记录 ID。",
            "related_messages": [0, 1],
            "is_duplicate": false,
            "duplicate_of_id": "",
            "primary_message_index": 0
        }
    ]
}

字段说明（请严格遵循）：
- result: 可选值为 none / suspected / confirmed 三者之一
- severity: 可选值为 low / medium / high / critical 四者之一
- related_messages: 数组，包含所有与此 bug 相关的消息索引（可能有多个）
- primary_message_index: 单个整数，必须是 related_messages 数组中的某一个索引，表示最关键的那一条
- is_duplicate: 布尔值，是否与已记录 bug 重复
- duplicate_of_id: 字符串，重复 bug 对应的已记录 ID

重要规则：
- 一次分析中可能包含多条独立的 bug 报告（不同用户报告不同的问题，或同一用户报告了多个不同的 bug）
- 请仔细区分不同的问题本质，将每个独立的 bug 作为一个单独的对象放入 bugs 数组
- 不要将多个不同的 bug 合并成一条描述，每个 bug 应有独立的 severity、summary、analysis 和 related_messages
- 如果群聊中确实存在多条不同来源的 bug 反馈，bugs 数组应包含所有独立的 bug 记录

primary_message_index 规则（重要）：
- primary_message_index 是导致你判断这是 bug 的最关键的一条消息索引
- 取值范围：-1 或 0 到消息总数减 1 之间的单个整数
- -1 表示聊天记录中没有哪条消息能明确指向 bug 证据（例如只是多人口头描述，没有具体报错）
- 0 及以上的值表示直接包含报错信息、异常描述或明确 bug 证据的那一条消息的索引
- primary_message_index 必须是 related_messages 数组中的一个索引，不要指向无关消息
- related_messages 可以包含多条相关消息，但 primary_message_index 只标出其中最关键的一条

判断标准：
- "none"：聊天记录完全是闲聊、技术讨论、使用教程、功能咨询等，没有任何 bug 报告
- "suspected"：聊天记录中有人提到遇到问题、报错、异常现象，但信息不足以确定是软件 bug（可能是配置错误、用户误操作、环境问题等）
- "confirmed"：聊天记录中有明确的错误信息（如报错日志、堆栈跟踪、明确的异常行为描述），且多人确认或复现

严重程度标准：
- "critical"：导致程序崩溃、数据丢失、安全漏洞
- "high"：核心功能无法使用，严重影响用户体验
- "medium"：部分功能异常，有 workaround
- "low"：UI 显示问题、轻微的异常行为、文档错误

去重规则（重要）：
- 如果发现的 bug 和"已记录的 bug 列表"中的某条在问题本质、影响范围、报错信息上高度相似，请设置 is_duplicate=true
- duplicate_of_id 填写已记录 bug 列表中对应记录的 id（注意：id 就是列表中每行末尾括号里的 ID）
- 如果是重复 bug，summary 保持简洁，analysis 中说明"该 bug 与记录 ID:xxx 相同，首次发现于 xxx"
- 不要仅仅因为同一现象被多人提到就判定为重复——只有本质相同的 bug 才是重复

注意：
- 不要对用户的正常技术讨论、功能请求、使用问题误判为 bug
- "我想要某个功能"属于功能请求，不是 bug
- "这个怎么用"属于使用问题，不是 bug
- "报错：xxx" "报错信息：xxx" 才是 bug 报告
- 注意区分"用户报告了 bug"和"bug 被确认了"——confirmed 需要有明确的错误证据
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
        existing_bugs: List[Dict[str, Any]] | None = None,
    ) -> AnalysisResult:
        """分析消息列表，返回结构化结果。"""
        if not messages:
            return AnalysisResult(result="none")

        prompt = self._build_prompt(messages, umo, existing_bugs)

        try:
            response = await self._call_llm(prompt, umo)
        except Exception as e:
            logger.error(f"[Analyzer] LLM 调用失败: {e}", exc_info=True)
            return AnalysisResult(error=str(e))

        response_text = getattr(response, "completion_text", "")
        if not response_text:
            logger.warning("[Analyzer] LLM 返回空文本")
            return AnalysisResult(raw_response="", error="empty response")

        result = self._parse_response(response_text)

        # 校验并修正 primary_message_index 范围
        if result.result != "none" and result.bugs:
            msg_count = len(messages)
            for bug in result.bugs:
                bug.related_messages = [
                    idx for idx in bug.related_messages if 0 <= idx < msg_count
                ]
                # bool 是 int 的子类，需要显式排除
                if not isinstance(bug.primary_message_index, int) or isinstance(
                    bug.primary_message_index, bool
                ):
                    bug.primary_message_index = -1
                elif not (-1 <= bug.primary_message_index < msg_count):
                    bug.primary_message_index = -1
                elif (
                    bug.primary_message_index >= 0
                    and bug.primary_message_index not in bug.related_messages
                ):
                    # PMI 必须在 related_messages 中（与 System Prompt 规则一致）
                    bug.primary_message_index = -1

        return result

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        messages: List[MessageRecord],
        umo: str,
        existing_bugs: List[Dict[str, Any]] | None = None,
    ) -> str:
        """构建 User Prompt，包含群信息、消息记录、已有 bug 列表。"""
        group_display = self._format_umo(umo)
        lines = [
            f"群聊信息：{group_display}",
            f"消息数量：{len(messages)} 条（按时间顺序）",
            "每条记录格式：[索引] 时间 发送者: 内容",
            "",
        ]

        for idx, msg in enumerate(messages):
            time_str = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))
            content = self._sanitize_content(msg.content)
            # 昵称可能包含换行符，不做处理会导致 Prompt 格式错乱（注入风险）
            sender_name = _one_line_text(msg.sender_name, "未知")
            lines.append(f"[{idx}] {time_str} {sender_name}: {content}")

        # 已有 bug 列表（用于去重），动态限制数量避免参考列表过长干扰分析
        if existing_bugs:
            max_existing = max(3, 15 - len(messages) // 20)
            existing_slice = existing_bugs[:max_existing]
            lines.append("")
            lines.append("已记录的 bug 列表（供参考，避免重复记录）：")
            for i, bug in enumerate(existing_slice, 1):
                status_str = f"[{_one_line_text(bug.get('status', 'open'), 'open')}]"
                # summary 可能包含换行符，替换避免破坏 Prompt 格式
                summary = _one_line_text(bug.get("summary", "") or "")
                severity = _one_line_text(bug.get("severity", "?"), "?")
                bug_id = _one_line_text(bug.get("id", "?"), "?")
                lines.append(
                    f"* {i}. [{severity}] {summary} {status_str} (ID: {bug_id})"
                )
            lines.append("")

        lines.append(
            "请分析这些记录中是否包含对 AstrBot 或其插件的 bug 反馈，"
            "输出 JSON 结果。注意区分新 bug 和已记录的重复 bug，"
            "如果存在多条独立的 bug 报告，请分别列出。"
        )
        return "\n".join(lines)

    @staticmethod
    def _sanitize_content(content: Any) -> str:
        """清洗消息内容中的图片/视频等多媒体信息，避免多模态模型报错或占用 token。"""
        content = _as_text(content)
        if not content:
            return "[空消息]"

        # 替换图片 URL
        content = _IMAGE_URL_RE.sub("[图片]", content)
        # 替换视频 URL
        content = _VIDEO_URL_RE.sub("[视频]", content)
        # 将消息内部换行压缩为空格，保持 prompt 每行语义完整
        content = _LINE_BREAK_RE.sub(" ", content)

        stripped = content.strip()
        # 如果清洗后只剩占位符（任意数量），保留
        if _EMPTY_AFTER_SANITIZE_RE.match(stripped):
            return stripped

        # 如果清洗后为空
        if not stripped:
            return "[空消息]"

        return content

    @staticmethod
    def _format_umo(umo: str) -> str:
        """将 UMO 格式化为可读群信息。"""
        parts = umo.split(":", 2)
        if len(parts) >= 3:
            session_id = _one_line_text(parts[2])
            return f"平台={parts[0]}, 群/会话 ID={session_id}"
        return _one_line_text(umo)

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

    @staticmethod
    def _extract_json_block(text: str) -> str | None:
        """从文本中提取 JSON 代码块或 JSON 对象。"""
        # 第 1/2 层：markdown 代码块
        patterns = [
            r"```json\s*(.*?)\s*```",  # ```json ... ```
            r"```\s*(.*?)\s*```",  # ``` ... ```
        ]
        for pat in patterns:
            match = re.search(pat, text, re.DOTALL)
            if match:
                extracted = match.group(1).strip()
                if extracted.startswith("{") or extracted.startswith("["):
                    return extracted

        # 第 3 层：用 JSONDecoder 从文本中找合法的 JSON 对象
        # 比正则贪婪/非贪婪匹配更可靠，能正确处理嵌套结构
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            idx = match.start()
            try:
                obj, end = decoder.raw_decode(text, idx)
                if isinstance(obj, dict) and "result" in obj:
                    # raw_decode 返回的 end 是相对于 text 的绝对索引
                    return text[idx:end]
            except json.JSONDecodeError:
                continue
        return None

    def _fix_json(self, text: str) -> str:
        """尝试修复常见的 JSON 格式问题。"""
        text = re.sub(r"```json\s*|\s*```", "", text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        return text.strip()

    def _extract_result(self, data: Any, raw: str) -> AnalysisResult:
        """从解析后的 dict 中提取结构化结果。"""
        result = AnalysisResult(raw_response=raw)

        if not isinstance(data, dict):
            result.error = "top-level JSON is not an object"
            return result

        result_str = _one_line_text(data.get("result", "none"), "none").lower()
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
            # 解析 primary_message_index，做基本类型容错
            # 拒绝 bool（JSON true/false）和 float（JSON 1.5 截断问题）
            pmi = bug_data.get("primary_message_index", -1)
            if pmi is None or isinstance(pmi, bool) or isinstance(pmi, float):
                pmi = -1
            else:
                try:
                    pmi = int(pmi)
                except (TypeError, ValueError):
                    pmi = -1

            # related_messages 必须是列表，否则后续遍历会 TypeError
            raw_rel = bug_data.get("related_messages", []) or []
            if not isinstance(raw_rel, list):
                raw_rel = []
            else:
                # 过滤掉 bool 和无法转为 int 的元素
                cleaned = []
                for v in raw_rel:
                    if isinstance(v, bool) or isinstance(v, float):
                        continue
                    try:
                        cleaned.append(int(v))
                    except (TypeError, ValueError):
                        continue
                raw_rel = cleaned

            bug = BugItem(
                severity=self._validate_severity(bug_data.get("severity", "medium")),
                summary=_one_line_text(bug_data.get("summary", "") or ""),
                analysis=_multiline_text(bug_data.get("analysis", "") or ""),
                related_messages=raw_rel,
                is_duplicate=_bool_value(bug_data.get("is_duplicate", False)),
                duplicate_of_id=_one_line_text(
                    bug_data.get("duplicate_of_id", "") or ""
                ),
                primary_message_index=pmi,
            )
            result.bugs.append(bug)

        return result

    @staticmethod
    def _validate_severity(value: Any) -> str:
        """验证 severity 值是否合法（大小写不敏感）。"""
        valid = {"low", "medium", "high", "critical"}
        if isinstance(value, str) and value.lower() in valid:
            return value.lower()
        return "medium"
