"""
集成测试 — 验证 main.py 的端到端消息处理流程。
"""

from __future__ import annotations

import asyncio
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
    async def test_inactive_before_initialize(self, plugin, mock_event):
        """initialize 完成前不应处理事件。"""
        plugin.config["global_mode"] = True

        with patch.object(
            plugin.buffer_mgr, "add_message", new_callable=AsyncMock
        ) as mock_add:
            await plugin.on_group_message(mock_event)

        mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_sets_active_after_loads(self, plugin):
        """数据加载完成后才进入 active 状态。"""
        states = []

        async def load_diagnostics():
            states.append(("diagnostics", plugin._active))

        async def load_bugs():
            states.append(("bugs", plugin._active))

        def start_scan():
            states.append(("scan", plugin._active))

        with patch.object(plugin.diagnostics, "load", side_effect=load_diagnostics):
            with patch.object(plugin.bug_store, "load", side_effect=load_bugs):
                with patch.object(plugin.buffer_mgr, "start_cleanup_task"):
                    with patch.object(
                        plugin, "_start_scan_task", side_effect=start_scan
                    ):
                        await plugin.initialize()

        assert states == [("diagnostics", False), ("bugs", False), ("scan", True)]
        assert plugin._active is True

    @pytest.mark.asyncio
    async def test_terminate(self, plugin):
        """终止应停止清理任务。"""
        with patch.object(
            plugin.buffer_mgr, "stop_cleanup_task", new_callable=AsyncMock
        ) as mock_stop:
            await plugin.terminate()
            mock_stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_terminate_drains_diagnostic_tasks(self, plugin):
        """终止时应等待后台诊断写入任务。"""
        finished = False

        async def wait_briefly():
            nonlocal finished
            await asyncio.sleep(0)
            finished = True

        task = asyncio.create_task(wait_briefly())
        plugin._diagnostic_tasks.add(task)
        with patch.object(
            plugin.buffer_mgr, "stop_cleanup_task", new_callable=AsyncMock
        ):
            await plugin.terminate()

        assert finished is True
        assert plugin._diagnostic_tasks == set()

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
        plugin._active = True
        plugin.config["global_mode"] = False
        plugin.config["umo_whitelist"] = ["other_umo"]

        with patch.object(plugin.buffer_mgr, "add_message") as mock_add:
            await plugin.on_group_message(mock_event)
            mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_group_message_processes_and_triggers(self, plugin, mock_event):
        """消息应被缓存并触发分析。"""
        plugin._active = True
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
                await asyncio.sleep(0)
                mock_add.assert_awaited_once()
                mock_analyze.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_group_message_handles_none_fields(self, plugin, mock_event):
        """事件字段为 None 时应转为空字符串/未知，不应在日志切片或入队时崩溃。"""
        plugin._active = True
        plugin.config["global_mode"] = True
        mock_event.get_sender_id.return_value = None
        mock_event.get_sender_name.return_value = None
        mock_event.get_message_outline.return_value = None

        with patch.object(
            plugin.buffer_mgr, "add_message", new_callable=AsyncMock
        ) as mock_add:
            from astrbot_plugin_bug_catcher.chat_buffer import AnalysisTrigger

            mock_add.return_value = AnalysisTrigger(triggered=False)
            await plugin.on_group_message(mock_event)
            mock_add.assert_awaited_once_with(
                umo=mock_event.unified_msg_origin,
                sender_id="",
                sender_name="未知",
                content="",
            )

    # ------------------------------------------------------------------
    # 分析与保存流程
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_analyze_and_store_confirmed(self, plugin, mock_context):
        """confirmed 结果应保存 bug 记录。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        plugin._active = True
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
                with patch.object(
                    plugin.buffer_mgr, "mark_analysis_complete", new_callable=AsyncMock
                ) as mock_complete:
                    await plugin._analyze_and_store("test_umo", messages)
                    mock_analyze.assert_awaited_once()
                    mock_save.assert_awaited_once()
                    mock_complete.assert_awaited_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_none(self, plugin):
        """none 结果不应保存，并应释放 in-flight 标记。"""
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
                with patch.object(
                    plugin.buffer_mgr, "mark_analysis_complete", new_callable=AsyncMock
                ) as mock_complete:
                    await plugin._analyze_and_store("test_umo", messages)
                    mock_save.assert_not_called()
                    mock_complete.assert_awaited_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_error(self, plugin):
        """分析异常应释放 in-flight 标记。"""
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
            with patch.object(
                plugin.buffer_mgr, "mark_analysis_complete", new_callable=AsyncMock
            ) as mock_complete:
                await plugin._analyze_and_store("test_umo", messages)
                mock_complete.assert_awaited_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_parse_error(self, plugin):
        """JSON 解析失败应释放 in-flight 标记。"""
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
            with patch.object(
                plugin.buffer_mgr, "mark_analysis_complete", new_callable=AsyncMock
            ) as mock_complete:
                await plugin._analyze_and_store("test_umo", messages)
                mock_complete.assert_awaited_once_with("test_umo")

    @pytest.mark.asyncio
    async def test_analyze_and_store_skips_save_after_terminate(self, plugin):
        """插件停用后，已返回的分析结果不应继续写入存储。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem
        from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

        result = AnalysisResult(
            result="confirmed",
            bugs=[BugItem(severity="high", summary="bug", analysis="x")],
        )
        messages = [
            MessageRecord(
                timestamp=1717450000.0,
                sender_id="u1",
                sender_name="用户",
                content="报错了",
            )
        ]

        async def analyze_and_deactivate(*args, **kwargs):
            plugin._active = False
            return result

        with patch.object(
            plugin.analyzer, "analyze", side_effect=analyze_and_deactivate
        ):
            with patch.object(
                plugin.bug_store, "add_bugs_from_analysis", new_callable=AsyncMock
            ) as mock_save:
                await plugin._analyze_and_store("test_umo", messages)
                mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_due_buffers_schedules_analysis(self, plugin):
        """主动扫描应为满足时间阈值的缓冲区创建分析任务。"""
        from astrbot_plugin_bug_catcher.chat_buffer import AnalysisTrigger

        plugin._active = True
        trigger = AnalysisTrigger(triggered=True, reason="time_threshold", messages=[])
        with patch.object(
            plugin.buffer_mgr, "collect_due_triggers", new_callable=AsyncMock
        ) as mock_collect:
            mock_collect.return_value = [("test_umo", trigger)]
            with patch.object(plugin, "_schedule_analysis") as mock_schedule:
                await plugin._scan_due_buffers_once()
                mock_schedule.assert_called_once_with("test_umo", trigger.messages)
