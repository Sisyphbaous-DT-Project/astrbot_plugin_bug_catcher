"""
集成测试 — 验证 main.py 的端到端消息处理流程。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot_plugin_bug_catcher.main import BugCatcherPlugin


@pytest.mark.integration
class TestBugCatcherPluginIntegration:
    """测试插件主类的完整流程。"""

    @pytest.fixture
    def plugin(self, mock_context, default_config):
        return BugCatcherPlugin(mock_context, default_config)

    @pytest.fixture
    def mock_event(self):
        """创建模拟的群聊消息事件。"""
        event = MagicMock()
        event.unified_msg_origin = "aiocqhttp:GROUP_MESSAGE:123456"
        event.get_sender_id.return_value = "user_001"
        event.get_sender_name.return_value = "测试用户"
        event.get_message_outline.return_value = "安装插件后报错了"
        return event

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_initialize(self, plugin):
        """初始化应加载数据和启动清理任务。"""
        with patch.object(
            plugin.bug_store, "load", new_callable=AsyncMock
        ) as mock_load:
            with patch.object(plugin.buffer_mgr, "start_cleanup_task") as mock_start:
                await plugin.initialize()
                mock_load.assert_awaited_once()
                mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminate(self, plugin):
        """终止应停止清理任务。"""
        with patch.object(plugin.buffer_mgr, "stop_cleanup_task") as mock_stop:
            await plugin.terminate()
            mock_stop.assert_called_once()

    # ------------------------------------------------------------------
    # 白名单过滤
    # ------------------------------------------------------------------

    def test_should_process_global_mode(self, mock_context):
        """全局模式应允许所有 UMO。"""
        plugin = BugCatcherPlugin(
            mock_context, {"global_mode": True, "umo_whitelist": []}
        )
        assert plugin._should_process("any_umo") is True

    def test_should_process_whitelist_match(self, mock_context):
        """白名单匹配应允许。"""
        plugin = BugCatcherPlugin(
            mock_context,
            {"global_mode": False, "umo_whitelist": ["allowed_umo"]},
        )
        assert plugin._should_process("allowed_umo") is True
        assert plugin._should_process("other_umo") is False

    # ------------------------------------------------------------------
    # 消息处理流程
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_on_group_message_ignores_non_whitelist(self, plugin, mock_event):
        """非白名单 UMO 应被忽略。"""
        plugin.config["global_mode"] = False
        plugin.config["umo_whitelist"] = ["other_umo"]

        with patch.object(plugin.buffer_mgr, "add_message") as mock_add:
            await plugin.on_group_message(mock_event)
            mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_group_message_processes_and_triggers(self, plugin, mock_event):
        """消息应被缓存并触发分析。"""
        plugin.config["global_mode"] = True
        plugin.config["batch_size"] = 3

        # 模拟触发分析
        from astrbot_plugin_bug_catcher.chat_buffer import AnalysisTrigger

        trigger = AnalysisTrigger(triggered=True, reason="batch_size", messages=[])

        with patch.object(
            plugin.buffer_mgr, "add_message", new_callable=AsyncMock
        ) as mock_add:
            mock_add.return_value = trigger
            with patch.object(
                plugin, "_analyze_and_store", new_callable=AsyncMock
            ) as mock_analyze:
                # 发送 1 条消息（batch_size=3，实际触发由 mock 控制）
                await plugin.on_group_message(mock_event)
                mock_add.assert_awaited_once()
                mock_analyze.assert_awaited_once()

    # ------------------------------------------------------------------
    # 分析与保存流程
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_analyze_and_store_confirmed(self, plugin, mock_context):
        """confirmed 结果应保存 bug 记录。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        result = AnalysisResult(
            result="confirmed",
            bugs=[
                BugItem(
                    severity="high",
                    summary="测试 bug",
                    analysis="测试分析",
                    related_messages=[0],
                )
            ],
        )
        messages = [
            MessageRecord(
                timestamp=1717450000.0,
                sender_id="u1",
                sender_name="用户",
                content="报错了",
            )
        ]

        with patch.object(
            plugin.analyzer, "analyze", new_callable=AsyncMock
        ) as mock_analyze:
            mock_analyze.return_value = result
            with patch.object(
                plugin.bug_store, "add_bugs_from_analysis", new_callable=AsyncMock
            ) as mock_save:
                with patch.object(plugin.buffer_mgr, "clear_buffer") as mock_clear:
                    await plugin._analyze_and_store("test_umo", messages)
                    mock_analyze.assert_awaited_once()
                    mock_save.assert_awaited_once()
                    mock_clear.assert_called_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_none(self, plugin):
        """none 结果应清空缓冲区但不保存。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        result = AnalysisResult(result="none")
        messages = [
            MessageRecord(
                timestamp=1717450000.0,
                sender_id="u1",
                sender_name="用户",
                content="闲聊",
            )
        ]

        with patch.object(
            plugin.analyzer, "analyze", new_callable=AsyncMock
        ) as mock_analyze:
            mock_analyze.return_value = result
            with patch.object(
                plugin.bug_store, "add_bugs_from_analysis", new_callable=AsyncMock
            ) as mock_save:
                with patch.object(plugin.buffer_mgr, "clear_buffer") as mock_clear:
                    await plugin._analyze_and_store("test_umo", messages)
                    mock_save.assert_not_called()
                    mock_clear.assert_called_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_error(self, plugin):
        """分析异常应清空缓冲区。"""
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        messages = [
            MessageRecord(
                timestamp=1717450000.0,
                sender_id="u1",
                sender_name="用户",
                content="msg",
            )
        ]

        with patch.object(
            plugin.analyzer, "analyze", new_callable=AsyncMock
        ) as mock_analyze:
            mock_analyze.side_effect = RuntimeError("LLM 失败")
            with patch.object(plugin.buffer_mgr, "clear_buffer") as mock_clear:
                await plugin._analyze_and_store("test_umo", messages)
                mock_clear.assert_called_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_parse_error(self, plugin):
        """JSON 解析失败应清空缓冲区。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        result = AnalysisResult(error="JSON 解析失败")
        messages = [
            MessageRecord(
                timestamp=1717450000.0,
                sender_id="u1",
                sender_name="用户",
                content="msg",
            )
        ]

        with patch.object(
            plugin.analyzer, "analyze", new_callable=AsyncMock
        ) as mock_analyze:
            mock_analyze.return_value = result
            with patch.object(plugin.buffer_mgr, "clear_buffer") as mock_clear:
                await plugin._analyze_and_store("test_umo", messages)
                mock_clear.assert_called_once_with("test_umo")
