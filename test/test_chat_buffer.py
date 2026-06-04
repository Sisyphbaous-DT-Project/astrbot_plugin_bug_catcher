"""
ChatBufferManager 单元测试。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from astrbot_plugin_bug_catcher.chat_buffer import ChatBufferManager


@pytest.mark.unit
class TestChatBufferManager:
    """测试消息缓存管理器的核心功能。"""

    @pytest.fixture
    def mgr(self, default_config):
        return ChatBufferManager(default_config)

    # ------------------------------------------------------------------
    # 基本入队
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_add_message_no_trigger(self, mgr):
        """未达到 batch_size 时不应触发分析。"""
        # batch_size=10, 添加 5 条消息
        for i in range(5):
            trigger = await mgr.add_message(
                umo="test:GROUP_MESSAGE:1",
                sender_id=f"u{i}",
                sender_name=f"用户{i}",
                content=f"msg {i}",
            )
        assert trigger.triggered is False
        assert mgr.get_buffer_size("test:GROUP_MESSAGE:1") == 5

    @pytest.mark.asyncio
    async def test_add_message_trigger_by_batch_size(self, mgr):
        """达到 batch_size 时应触发分析。"""
        trigger = None
        for i in range(10):
            trigger = await mgr.add_message(
                umo="test:GROUP_MESSAGE:1",
                sender_id=f"u{i}",
                sender_name=f"用户{i}",
                content=f"msg {i}",
            )
        assert trigger is not None
        assert trigger.triggered is True
        assert trigger.reason == "batch_size"
        assert len(trigger.messages) == 10

    @pytest.mark.asyncio
    async def test_add_message_multiple_umo(self, mgr):
        """不同 UMO 应独立缓存。"""
        for i in range(5):
            await mgr.add_message(
                umo="umo_a",
                sender_id="u1",
                sender_name="用户A",
                content=f"msg {i}",
            )
        for i in range(3):
            await mgr.add_message(
                umo="umo_b",
                sender_id="u2",
                sender_name="用户B",
                content=f"msg {i}",
            )
        assert mgr.get_buffer_size("umo_a") == 5
        assert mgr.get_buffer_size("umo_b") == 3

    # ------------------------------------------------------------------
    # FIFO 淘汰
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_fifo_eviction(self, mgr):
        """超过 max_history 时最旧消息应被淘汰。"""
        # max_history=20, 添加 25 条
        for i in range(25):
            await mgr.add_message(
                umo="test",
                sender_id="u1",
                sender_name="用户",
                content=f"msg {i}",
            )
        assert mgr.get_buffer_size("test") == 20

    # ------------------------------------------------------------------
    # 冷却检查
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cooldown_prevents_trigger(self, mgr):
        """冷却期内不应重复触发。"""
        # 第一次触发
        for i in range(10):
            trigger = await mgr.add_message(
                umo="test",
                sender_id="u1",
                sender_name="用户",
                content=f"msg {i}",
            )
        assert trigger.triggered is True

        # 清空缓冲区（模拟分析完成后的行为）
        mgr.clear_buffer("test")

        # 立即再添加 10 条（在冷却期内）
        for i in range(10, 20):
            trigger = await mgr.add_message(
                umo="test",
                sender_id="u1",
                sender_name="用户",
                content=f"msg {i}",
            )
        assert trigger.triggered is False

    # ------------------------------------------------------------------
    # 时间阈值触发（低活跃群聊）
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_time_threshold_trigger(self, mgr):
        """超过 time_threshold_min 应触发分析。"""
        # 添加少量消息（不足 batch_size）
        for i in range(3):
            await mgr.add_message(
                umo="test",
                sender_id="u1",
                sender_name="用户",
                content=f"msg {i}",
            )

        # 伪造上次分析时间为 10 分钟前（time_threshold_min=5）
        mgr._last_analysis["test"] = time.time() - 10 * 60

        trigger = await mgr.add_message(
            umo="test",
            sender_id="u1",
            sender_name="用户",
            content="trigger msg",
        )
        assert trigger.triggered is True
        assert trigger.reason == "time_threshold"

    # ------------------------------------------------------------------
    # 缓冲区操作
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_clear_buffer(self, mgr):
        """清空缓冲区后大小应为 0。"""
        for i in range(5):
            await mgr.add_message(
                umo="test",
                sender_id="u1",
                sender_name="用户",
                content=f"msg {i}",
            )
        mgr.clear_buffer("test")
        assert mgr.get_buffer_size("test") == 0

    @pytest.mark.asyncio
    async def test_get_all_stats(self, mgr):
        """统计信息应包含所有 UMO。"""
        await mgr.add_message("umo_a", "u1", "A", "msg")
        await mgr.add_message("umo_b", "u2", "B", "msg")
        stats = mgr.get_all_stats()
        assert "umo_a" in stats
        assert "umo_b" in stats

    # ------------------------------------------------------------------
    # 并发安全
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_concurrent_add_message(self, mgr):
        """并发入队不应丢失消息（受 maxlen 限制）。"""

        async def add_many(umo: str, start: int, count: int):
            for i in range(start, start + count):
                await mgr.add_message(
                    umo=umo,
                    sender_id="u1",
                    sender_name="用户",
                    content=f"msg {i}",
                )

        # 同时从两个任务入队（max_history=20，最终只保留 20 条）
        await asyncio.gather(
            add_many("test", 0, 50),
            add_many("test", 50, 50),
        )
        # 验证 deque maxlen 生效
        assert mgr.get_buffer_size("test") == 20  # max_history=20
        # 验证保留的是最新的消息
        buf = mgr._buffers["test"]
        assert buf[-1].content == "msg 99"

    # ------------------------------------------------------------------
    # TTL 清理
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cleanup_expired_buffers(self, mgr):
        """过期缓冲区应被清理。"""
        await mgr.add_message("old_umo", "u1", "用户", "msg")
        # 伪造最后活跃时间为 25 小时前
        mgr._last_active["old_umo"] = time.time() - 25 * 3600
        await mgr._do_cleanup()
        assert mgr.get_buffer_size("old_umo") == 0
        assert "old_umo" not in mgr._buffers

    @pytest.mark.asyncio
    async def test_cleanup_keeps_active_buffers(self, mgr):
        """活跃缓冲区不应被清理。"""
        await mgr.add_message("active_umo", "u1", "用户", "msg")
        await mgr._do_cleanup()
        assert mgr.get_buffer_size("active_umo") == 1
