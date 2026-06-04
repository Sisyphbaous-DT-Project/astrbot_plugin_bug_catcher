"""
BugStore 单元测试。
"""

from __future__ import annotations

import json
import os

import pytest

from astrbot_plugin_bug_catcher.bug_store import BugStore
from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem
from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord


@pytest.mark.unit
class TestBugStore:
    """测试 Bug 持久化存储的核心功能。"""

    @pytest.fixture
    def store(self, temp_data_dir):
        data_dir = temp_data_dir / "plugin_bug_catcher"
        return BugStore(data_dir=str(data_dir))

    @pytest.fixture
    def sample_analysis(self):
        return AnalysisResult(
            result="confirmed",
            bugs=[
                BugItem(
                    severity="high",
                    summary="模块加载失败",
                    analysis="用户报告安装后报错",
                    related_messages=[0, 1],
                    primary_message_index=0,
                ),
                BugItem(
                    severity="medium",
                    summary="配置项缺失",
                    analysis="缺少必要配置",
                    related_messages=[2],
                    primary_message_index=1,
                ),
            ],
        )

    @pytest.fixture
    def sample_messages(self):
        return [
            MessageRecord(
                timestamp=1717450000.0,
                sender_id="u1",
                sender_name="用户A",
                content="报错了",
            ),
            MessageRecord(
                timestamp=1717450060.0,
                sender_id="u2",
                sender_name="用户B",
                content="什么错误？",
            ),
        ]

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_load_empty(self, store):
        """无数据文件时应初始化空存储。"""
        await store.load()
        assert len(store._bugs) == 0

    @pytest.mark.asyncio
    async def test_load_existing_data(
        self, store, sample_analysis, sample_messages, temp_data_dir
    ):
        """应正确加载已有数据。"""
        await store.add_bugs_from_analysis(
            umo="test:GROUP_MESSAGE:1",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        # 创建新实例加载同一文件
        data_dir = temp_data_dir / "plugin_bug_catcher"
        store2 = BugStore(data_dir=str(data_dir))
        await store2.load()
        assert len(store2._bugs) == 2

    @pytest.mark.asyncio
    async def test_load_corrupted_json(self, store, temp_data_dir):
        """损坏的 JSON 不应导致崩溃。"""
        # 写入损坏的 JSON
        os.makedirs(temp_data_dir / "plugin_bug_catcher", exist_ok=True)
        with open(temp_data_dir / "plugin_bug_catcher" / "bugs.json", "w") as f:
            f.write("这不是 JSON")
        await store.load()
        assert len(store._bugs) == 0

    @pytest.mark.asyncio
    async def test_load_non_dict_data(self, store, temp_data_dir):
        """非 dict 顶层数据应优雅处理。"""
        os.makedirs(temp_data_dir / "plugin_bug_catcher", exist_ok=True)
        with open(temp_data_dir / "plugin_bug_catcher" / "bugs.json", "w") as f:
            json.dump([1, 2, 3], f)
        await store.load()
        assert len(store._bugs) == 0

    @pytest.mark.asyncio
    async def test_load_non_list_bugs(self, store, temp_data_dir):
        """bugs 字段非 list 时应回退为空列表。"""
        os.makedirs(temp_data_dir / "plugin_bug_catcher", exist_ok=True)
        with open(temp_data_dir / "plugin_bug_catcher" / "bugs.json", "w") as f:
            json.dump({"version": 1, "bugs": "not_a_list", "stats": {}}, f)
        await store.load()
        assert len(store._bugs) == 0

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_add_bugs_from_analysis(
        self, store, sample_analysis, sample_messages
    ):
        """应正确添加并保存 bug 记录。"""
        records = await store.add_bugs_from_analysis(
            umo="test:GROUP_MESSAGE:1",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        assert len(records) == 2
        assert records[0].result == "confirmed"
        assert records[0].severity == "high"
        assert len(records[0].raw_messages) == 2

    @pytest.mark.asyncio
    async def test_add_none_result(self, store):
        """result=none 时不应添加记录。"""
        records = await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=AnalysisResult(result="none"),
            raw_messages=[],
        )
        assert len(records) == 0

    @pytest.mark.asyncio
    async def test_add_bugs_primary_message_index(self, store, sample_messages):
        """应正确存储 primary_message_index 并用其定位报告者。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem

        analysis = AnalysisResult(
            result="confirmed",
            bugs=[
                BugItem(
                    severity="high",
                    summary="崩溃",
                    analysis="x",
                    related_messages=[1],
                    primary_message_index=1,  # 应指向 用户B
                )
            ],
        )
        records = await store.add_bugs_from_analysis(
            umo="test:GROUP_MESSAGE:1",
            analysis_result=analysis,
            raw_messages=sample_messages,
        )
        assert len(records) == 1
        assert records[0].primary_message_index == 1
        # 报告者应从 primary_message_index 指向的消息获取
        assert records[0].report_history[0]["reporter_name"] == "用户B"

    @pytest.mark.asyncio
    async def test_add_bugs_primary_message_index_fallback(
        self, store, sample_messages
    ):
        """PMI 无效时应回退到 related_messages 定位报告者。"""
        from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem

        analysis = AnalysisResult(
            result="confirmed",
            bugs=[
                BugItem(
                    severity="high",
                    summary="崩溃",
                    analysis="x",
                    related_messages=[1],  # 用户B
                    primary_message_index=-1,  # 无效，应回退到 related_messages
                )
            ],
        )
        records = await store.add_bugs_from_analysis(
            umo="test:GROUP_MESSAGE:1",
            analysis_result=analysis,
            raw_messages=sample_messages,
        )
        assert len(records) == 1
        assert records[0].primary_message_index == -1
        # 回退到 related_messages[0] 指向的用户B
        assert records[0].report_history[0]["reporter_name"] == "用户B"

    @pytest.mark.asyncio
    async def test_get_bugs_pagination(self, store, sample_analysis, sample_messages):
        """分页查询应正确工作。"""
        for i in range(25):
            await store.add_bugs_from_analysis(
                umo=f"test_{i}",
                analysis_result=sample_analysis,
                raw_messages=sample_messages,
            )
        bugs, total = await store.get_bugs(page=1, page_size=10)
        assert len(bugs) == 10
        assert total == 50  # 25 * 2 bugs each

    @pytest.mark.asyncio
    async def test_get_bugs_filter_by_severity(
        self, store, sample_analysis, sample_messages
    ):
        """按严重程度筛选应正确工作。"""
        await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        bugs, total = await store.get_bugs(severity="high")
        assert total == 1
        assert bugs[0].severity == "high"

    @pytest.mark.asyncio
    async def test_get_bugs_filter_by_status(
        self, store, sample_analysis, sample_messages
    ):
        """按状态筛选应正确工作。"""
        await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        # 更新状态
        bug_id = list(store._bugs.keys())[0]
        await store.update_bug_status(bug_id, "resolved")

        open_bugs, _ = await store.get_bugs(status="open")
        resolved_bugs, _ = await store.get_bugs(status="resolved")
        assert len(open_bugs) == 1
        assert len(resolved_bugs) == 1

    @pytest.mark.asyncio
    async def test_get_bug_by_id(self, store, sample_analysis, sample_messages):
        """按 ID 查询应返回正确记录。"""
        records = await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        bug_id = records[0].id
        found = await store.get_bug_by_id(bug_id)
        assert found is not None
        assert found.id == bug_id

    @pytest.mark.asyncio
    async def test_get_bug_by_id_not_found(self, store):
        """不存在的 ID 应返回 None。"""
        found = await store.get_bug_by_id("non-existent")
        assert found is None

    @pytest.mark.asyncio
    async def test_update_status(self, store, sample_analysis, sample_messages):
        """更新状态应正确工作。"""
        records = await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        bug_id = records[0].id
        success = await store.update_bug_status(bug_id, "resolved", note="已修复")
        assert success is True

        bug = await store.get_bug_by_id(bug_id)
        assert bug.status == "resolved"
        assert bug.note == "已修复"
        assert bug.resolved_at is not None

    @pytest.mark.asyncio
    async def test_update_status_invalid(self, store, sample_analysis, sample_messages):
        """无效状态应拒绝更新。"""
        records = await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        bug_id = records[0].id
        success = await store.update_bug_status(bug_id, "invalid_status")
        assert success is False

    @pytest.mark.asyncio
    async def test_delete_bug(self, store, sample_analysis, sample_messages):
        """删除应正确工作。"""
        records = await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        bug_id = records[0].id
        success = await store.delete_bug(bug_id)
        assert success is True
        assert await store.get_bug_by_id(bug_id) is None

    @pytest.mark.asyncio
    async def test_delete_bug_not_found(self, store):
        """删除不存在的记录应返回 False。"""
        success = await store.delete_bug("non-existent")
        assert success is False

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_stats(self, store, sample_analysis, sample_messages):
        """统计信息应正确累积。"""
        await store.add_bugs_from_analysis(
            umo="test",
            analysis_result=sample_analysis,
            raw_messages=sample_messages,
        )
        stats = await store.get_stats()
        assert stats["total_confirmed"] == 2
        assert stats["total_analyzed"] == 1

    # ------------------------------------------------------------------
    # 并发安全
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_concurrent_add(self, store, sample_analysis, sample_messages):
        """并发添加不应丢失数据。"""
        import asyncio

        async def add_batch(i: int):
            return await store.add_bugs_from_analysis(
                umo=f"batch_{i}",
                analysis_result=sample_analysis,
                raw_messages=sample_messages,
            )

        results = await asyncio.gather(*[add_batch(i) for i in range(10)])
        total_bugs = sum(len(r) for r in results)
        assert total_bugs == 20  # 10 batches * 2 bugs each

    # ------------------------------------------------------------------
    # 防御性容错
    # ------------------------------------------------------------------

    def test_find_primary_message_with_string_indices(self, sample_messages):
        """related_messages 包含字符串索引时应正确解析。"""
        from astrbot_plugin_bug_catcher.bug_store import BugStore

        msg = BugStore._find_primary_message(["0", 1, "abc"], sample_messages)
        assert msg is not None
        assert msg.sender_name == "用户A"

    def test_find_primary_message_with_invalid_indices(self, sample_messages):
        """related_messages 全为无效索引时应回退到首条消息。"""
        from astrbot_plugin_bug_catcher.bug_store import BugStore

        msg = BugStore._find_primary_message(["xyz", -1, 99], sample_messages)
        assert msg is not None
        assert msg.sender_name == "用户A"
