"""
DiagnosticsStore 单元测试。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_bug_catcher.diagnostics import DiagnosticsStore


@pytest.mark.unit
class TestDiagnosticsStore:
    """测试插件诊断日志存储。"""

    @pytest.mark.asyncio
    async def test_record_and_summary(self, temp_data_dir):
        """记录 warning/error 后摘要应显示未读计数。"""
        store = DiagnosticsStore(
            {"diagnostics_max_entries": 20},
            data_dir=str(temp_data_dir),
        )

        await store.record_warning("警告", "provider returned empty response")
        await store.record_error("错误", RuntimeError("save failed"))

        summary = await store.get_summary()
        assert summary["status"] == "error"
        assert summary["unread_warning_count"] == 1
        assert summary["unread_error_count"] == 1

        events = await store.list_events(limit=10)
        assert len(events) == 2
        assert events[0]["level"] == "error"

    @pytest.mark.asyncio
    async def test_mark_read_and_clear(self, temp_data_dir):
        """应支持标记已读和清空。"""
        store = DiagnosticsStore(data_dir=str(temp_data_dir))
        await store.record_error("错误", "boom")

        marked = await store.mark_read()
        summary = await store.get_summary()
        assert marked == 1
        assert summary["status"] == "ok"

        cleared = await store.clear()
        assert cleared == 1
        assert await store.list_events() == []

    @pytest.mark.asyncio
    async def test_persist_and_trim(self, temp_data_dir):
        """诊断事件应持久化并按最大条数裁剪。"""
        store = DiagnosticsStore(
            {"diagnostics_max_entries": 20},
            data_dir=str(temp_data_dir),
        )
        for i in range(25):
            await store.record_warning(f"警告{i}", "msg")

        reloaded = DiagnosticsStore(
            {"diagnostics_max_entries": 20},
            data_dir=str(temp_data_dir),
        )
        await reloaded.load()
        events = await reloaded.list_events(limit=30)

        assert len(events) == 20
        assert events[-1]["title"] == "警告5"
