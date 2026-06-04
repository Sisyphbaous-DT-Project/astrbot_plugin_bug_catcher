"""
BugAnalyzer 单元测试。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from astrbot_plugin_bug_catcher.analyzer import BugAnalyzer


@pytest.mark.unit
class TestBugAnalyzer:
    """测试 AI 分析引擎的核心功能。"""

    @pytest.fixture
    def analyzer(self, mock_context, default_config):
        return BugAnalyzer(mock_context, default_config)

    @pytest.fixture
    def sample_messages(self):
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        return [
            MessageRecord(
                timestamp=1717450000.0 + i * 60,
                sender_id=f"user_{i:03d}",
                sender_name=f"用户{i}",
                content="安装插件后报错了" if i == 0 else f"消息内容 {i}",
            )
            for i in range(5)
        ]

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    def test_build_prompt_format(self, analyzer, sample_messages):
        """Prompt 应包含正确格式的消息列表。"""
        prompt = analyzer._build_prompt(sample_messages)
        assert "以下是近期 5 条群聊记录" in prompt
        assert "[0]" in prompt
        assert "[4]" in prompt
        assert "安装插件后报错了" in prompt
        assert "请分析这些记录" in prompt

    # ------------------------------------------------------------------
    # Token 截断
    # ------------------------------------------------------------------

    def test_truncate_not_needed(self, analyzer, sample_messages):
        """短 Prompt 不应被截断。"""
        prompt = analyzer._build_prompt(sample_messages)
        result = analyzer._truncate_if_needed(prompt)
        assert result == prompt

    def test_truncate_removes_oldest(self, analyzer):
        """超长 Prompt 应截断最早的消息。"""
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        # 创建大量消息使 Prompt 超长
        messages = [
            MessageRecord(
                timestamp=1717450000.0 + i,
                sender_id="u1",
                sender_name="用户",
                content="A" * 500,  # 长消息加速超限
            )
            for i in range(200)
        ]
        prompt = analyzer._build_prompt(messages)
        truncated = analyzer._truncate_if_needed(prompt)
        assert len(truncated) < len(prompt)
        # 尾部指令应保留
        assert "请分析这些记录" in truncated

    # ------------------------------------------------------------------
    # JSON 解析
    # ------------------------------------------------------------------

    def test_parse_valid_json(self, analyzer):
        """标准 JSON 应正确解析。"""
        text = (
            '{"result": "confirmed", "bugs": [{"severity": "high", '
            '"summary": "崩溃", "analysis": "复现步骤", "related_messages": [0]}]}'
        )
        result = analyzer._parse_response(text)
        assert result.result == "confirmed"
        assert len(result.bugs) == 1
        assert result.bugs[0].severity == "high"
        assert result.bugs[0].summary == "崩溃"
        assert result.error == ""

    def test_parse_none_result(self, analyzer):
        """result=none 时应返回空 bugs 列表。"""
        text = '{"result": "none", "bugs": []}'
        result = analyzer._parse_response(text)
        assert result.result == "none"
        assert len(result.bugs) == 0

    def test_parse_json_in_markdown(self, analyzer):
        """Markdown 代码块包裹的 JSON 应正确提取。"""
        text = (
            '```json\n{"result": "suspected", "bugs": [{"severity": "medium", '
            '"summary": "可能的问题", "analysis": "需要确认", "related_messages": [1]}]}\n```'
        )
        result = analyzer._parse_response(text)
        assert result.result == "suspected"
        assert len(result.bugs) == 1

    def test_parse_json_with_trailing_comma(self, analyzer):
        """尾部逗号应被修复。"""
        text = '{"result": "confirmed", "bugs": [{"severity": "low", "summary": "x", "analysis": "y", "related_messages": [0],}]}'
        result = analyzer._parse_response(text)
        assert result.result == "confirmed"
        assert len(result.bugs) == 1

    def test_parse_invalid_json(self, analyzer):
        """完全无法解析时应标记 error。"""
        text = "这根本不是 JSON"
        result = analyzer._parse_response(text)
        assert result.result == "none"
        assert result.error == "JSON 解析失败"
        assert result.raw_response == text

    def test_parse_missing_bugs_field(self, analyzer):
        """缺失 bugs 字段时应返回空列表。"""
        text = '{"result": "confirmed"}'
        result = analyzer._parse_response(text)
        assert result.result == "confirmed"
        assert len(result.bugs) == 0

    def test_parse_invalid_result_value(self, analyzer):
        """非法 result 值应标记 error。"""
        text = '{"result": "maybe", "bugs": []}'
        result = analyzer._parse_response(text)
        assert result.error != ""
        assert "invalid result value" in result.error

    # ------------------------------------------------------------------
    # 字段校验
    # ------------------------------------------------------------------

    def test_validate_severity_valid(self, analyzer):
        """合法 severity 应被保留。"""
        assert analyzer._validate_severity("critical") == "critical"
        assert analyzer._validate_severity("high") == "high"

    def test_validate_severity_invalid(self, analyzer):
        """非法 severity 应回退为 medium。"""
        assert analyzer._validate_severity("unknown") == "medium"
        assert analyzer._validate_severity(123) == "medium"
        assert analyzer._validate_severity(None) == "medium"

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_call_llm_with_configured_provider(self, analyzer, mock_context):
        """配置了 provider_id 时应直接使用。"""
        analyzer.provider_id = "gpt4"
        mock_context.llm_generate.return_value = MagicMock(completion_text='{"result": "none"}')
        await analyzer._call_llm("test prompt", "test_umo")
        mock_context.llm_generate.assert_awaited_once()
        call_kwargs = mock_context.llm_generate.call_args.kwargs
        assert call_kwargs["chat_provider_id"] == "gpt4"

    @pytest.mark.asyncio
    async def test_call_llm_fallback_to_default(
        self, analyzer, mock_context
    ):
        """未配置 provider_id 时应回退到默认 Provider。"""
        analyzer.provider_id = ""
        mock_provider = MagicMock()
        mock_provider.meta.return_value = MagicMock(id="default_gpt")
        mock_context.get_using_provider.return_value = mock_provider
        mock_context.llm_generate.return_value = MagicMock(completion_text='{"result": "none"}')

        await analyzer._call_llm("test prompt", "test_umo")
        mock_context.get_using_provider.assert_called_once_with("test_umo")
        call_kwargs = mock_context.llm_generate.call_args.kwargs
        assert call_kwargs["chat_provider_id"] == "default_gpt"

    @pytest.mark.asyncio
    async def test_call_llm_no_provider_raises(
        self, analyzer, mock_context
    ):
        """无 Provider 时应抛出异常。"""
        analyzer.provider_id = ""
        mock_context.get_using_provider.return_value = None
        with pytest.raises(RuntimeError, match="未配置 Provider"):
            await analyzer._call_llm("test prompt", "test_umo")

    # ------------------------------------------------------------------
    # 端到端 analyze
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_analyze_empty_messages(self, analyzer):
        """空消息列表应返回 none。"""
        result = await analyzer.analyze([], "test")
        assert result.result == "none"

    @pytest.mark.asyncio
    async def test_analyze_success(
        self, analyzer, mock_context, sample_messages, mock_llm_response
    ):
        """正常分析流程。"""
        analyzer.provider_id = "test_provider"
        mock_context.llm_generate.return_value = mock_llm_response

        result = await analyzer.analyze(sample_messages, "test_umo")

        assert result.result == "confirmed"
        assert len(result.bugs) == 1
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_analyze_llm_failure(
        self, analyzer, mock_context, sample_messages
    ):
        """LLM 调用失败应返回 error。"""
        analyzer.provider_id = "test_provider"
        mock_context.llm_generate.side_effect = ConnectionError("网络超时")

        result = await analyzer.analyze(sample_messages, "test_umo")
        assert result.error != ""
        assert "网络超时" in result.error

    @pytest.mark.asyncio
    async def test_analyze_empty_response(
        self, analyzer, mock_context, sample_messages
    ):
        """LLM 返回空文本应返回 error。"""
        analyzer.provider_id = "test_provider"
        mock_context.llm_generate.return_value = MagicMock(completion_text="")

        result = await analyzer.analyze(sample_messages, "test_umo")
        assert result.error == "empty response"



