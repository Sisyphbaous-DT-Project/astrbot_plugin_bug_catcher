"""
兜底模拟测试 — 验证各种异常/边界场景的处理结果。

运行: pytest test/test_resilience.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

import pytest

from astrbot_plugin_bug_catcher.analyzer import BugAnalyzer, AnalysisResult, BugItem
from astrbot_plugin_bug_catcher.bug_store import BugStore
from astrbot_plugin_bug_catcher.chat_buffer import ChatBufferManager, MessageRecord
from astrbot_plugin_bug_catcher.dashboard_api import _int_param


@pytest.mark.resilience
class TestResilienceDemo:
    """演示各种兜底场景的处理结果。"""

    # ------------------------------------------------------------------
    # 1. Analyzer — LLM 畸形输出解析兜底
    # ------------------------------------------------------------------

    def test_analyzer_various_malformed_outputs(self, mock_context, default_config):
        """LLM 返回各种畸形输出时的解析兜底。"""
        analyzer = BugAnalyzer(mock_context, default_config)

        cases = [
            (
                "标准 JSON",
                '{"result":"confirmed","bugs":[{"severity":"high","summary":"崩溃","analysis":"堆栈","related_messages":[0]}]}',
                "confirmed",
                0,
                "",
            ),
            (
                "Markdown 包裹",
                '```json\n{"result":"suspected","bugs":[{"severity":"medium","summary":"可能","analysis":"?","related_messages":[1]}]}\n```',
                "suspected",
                0,
                "",
            ),
            (
                "尾部逗号",
                '{"result":"confirmed","bugs":[{"severity":"low","summary":"x","analysis":"y","related_messages":[0],}]}',
                "confirmed",
                0,
                "",
            ),
            (
                "纯文本夹杂 JSON",
                '分析结果如下：\n```\n{"result":"confirmed","bugs":[{"severity":"high","summary":"bug","analysis":"细节","related_messages":[0,1]}]}\n```\n希望对你有帮助。',
                "confirmed",
                0,
                "",
            ),
            ("完全不是 JSON", "这根本不是 JSON 格式", "none", 1, "JSON 解析失败"),
            ("缺失 bugs 字段", '{"result":"confirmed"}', "confirmed", 0, ""),
            (
                "非法 result 值",
                '{"result":"maybe","bugs":[]}',
                "none",
                1,
                "invalid result value",
            ),
            (
                "related_messages 含字符串+数字 duplicate_of_id",
                '{"result":"confirmed","bugs":[{"severity":"medium","summary":"test","analysis":"a","related_messages":["0","abc",1],"is_duplicate":true,"duplicate_of_id":999}]}',
                "confirmed",
                0,
                "",
            ),
        ]

        print("\n  [Analyzer] LLM 畸形输出解析兜底:")
        for name, text, expected_result, has_error, error_substr in cases:
            result = analyzer._parse_response(text)
            ok = result.result == expected_result
            err_ok = (has_error == 0 and not result.error) or (
                has_error == 1 and error_substr in result.error
            )
            dup_id = result.bugs[0].duplicate_of_id if result.bugs else ""
            status = "✅" if ok and err_ok else "❌"
            print(
                f"    {status} {name:25s} → result={result.result:10s} error={result.error!r:25s} dup_id={dup_id!r}"
            )
            assert ok, f"{name}: expected result={expected_result}, got {result.result}"
            assert err_ok, f"{name}: error mismatch, got {result.error!r}"

    # ------------------------------------------------------------------
    # 2. ChatBuffer — 异常配置值防御
    # ------------------------------------------------------------------

    def test_chat_buffer_config_clamping(self):
        """异常配置值时的防御性 clamp。"""
        print("\n  [ChatBuffer] 异常配置值防御:")

        cases = [
            ({"batch_size": -50, "max_history": 300}, 1, 300),
            ({"batch_size": 500, "max_history": 300}, 300, 300),
            (
                {"batch_size": 200, "max_history": -10},
                10,
                10,
            ),  # max_history clamped to 10, then batch_size capped to max_history
            (
                {
                    "batch_size": 10,
                    "max_history": 20,
                    "time_threshold_min": 0,
                    "analysis_interval_min": -3,
                },
                10,
                20,
            ),
        ]

        for cfg, expected_bs, expected_mh in cases:
            mgr = ChatBufferManager(cfg)
            print(
                f"    ✅ 输入 batch={cfg.get('batch_size')}, max_hist={cfg.get('max_history')}"
                f" → 实际 batch={mgr.batch_size}, max_hist={mgr.max_history}"
            )
            assert mgr.batch_size == expected_bs
            assert mgr.max_history == expected_mh

    # ------------------------------------------------------------------
    # 3. ChatBuffer — 新 UMO 时间阈值触发
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_new_umo_time_threshold(self):
        """新 UMO 通过时间阈值触发分析。"""
        print("\n  [ChatBuffer] 新 UMO 时间阈值触发:")

        mgr = ChatBufferManager(
            {
                "batch_size": 200,
                "max_history": 300,
                "time_threshold_min": 5,
                "analysis_interval_min": 1,
            }
        )

        # 添加 2 条消息
        for i in range(2):
            await mgr.add_message("new_umo", f"u{i}", f"用户{i}", f"msg {i}")

        # 把最早消息拨到 10 分钟前
        buf = mgr._buffers["new_umo"]
        old = buf[0]
        buf[0] = MessageRecord(
            timestamp=time.time() - 10 * 60,
            sender_id=old.sender_id,
            sender_name=old.sender_name,
            content=old.content,
        )

        trigger = await mgr.add_message("new_umo", "u2", "用户2", "trigger msg")
        print(
            f"    ✅ 新 UMO 首条消息 10min 前 → triggered={trigger.triggered}, reason={trigger.reason}"
        )
        assert trigger.triggered is True
        assert trigger.reason == "time_threshold"

    # ------------------------------------------------------------------
    # 4. BugStore — 数据损坏与边界兜底
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_bug_store_corruption_handling(self):
        """BugStore 数据损坏/异常场景。"""
        print("\n  [BugStore] 数据损坏与边界兜底:")

        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = os.path.join(tmpdir, "bugs.json")

            # 4.1 损坏的 JSON
            with open(bad_file, "w") as f:
                f.write("{这不是 JSON")
            store = BugStore(data_dir=tmpdir)
            await store.load()
            print(f"    ✅ 损坏 JSON → 加载成功，{len(store._bugs)} 条记录")
            assert len(store._bugs) == 0

            # 4.2 非 dict 根
            with open(bad_file, "w") as f:
                json.dump([1, 2, 3], f)
            store2 = BugStore(data_dir=tmpdir)
            await store2.load()
            print(f"    ✅ 非 dict 根 → 加载成功，{len(store2._bugs)} 条记录")
            assert len(store2._bugs) == 0

            # 4.3 旧版本数据（无 report_history，缺少部分必填字段）
            with open(bad_file, "w") as f:
                json.dump(
                    {
                        "version": 1,
                        "bugs": [
                            {
                                "id": "old-1",
                                "umo": "test",
                                "summary": "旧数据",
                                "severity": "high",
                                "status": "open",
                                "result": "confirmed",
                                "umo_display": "test",
                                "platform": "qq",
                                "created_at": "2024-01-01T00:00:00",
                                "analysis": "",
                                "related_messages": [],
                                "raw_messages": [],
                            }
                        ],
                        "stats": {"total_confirmed": 1},
                    },
                    f,
                )
            store3 = BugStore(data_dir=tmpdir)
            await store3.load()
            print(f"    ✅ v1 旧数据 → 加载成功，{len(store3._bugs)} 条记录")
            assert len(store3._bugs) == 1

            # 4.4 删除不存在记录
            result = await store3.delete_bug("non-existent")
            print(f"    ✅ 删除不存在记录 → {result}")
            assert result is False

            # 4.5 更新非法状态
            bug = list(store3._bugs.values())[0]
            result = await store3.update_bug_status(bug.id, "invalid_status")
            print(f"    ✅ 更新非法状态 → {result}")
            assert result is False

            # 4.6 重复合并（整数 duplicate_of_id → 强制 str，不存在则新建）
            result = AnalysisResult(
                result="confirmed",
                bugs=[
                    BugItem(
                        severity="high",
                        summary="重复",
                        analysis="a",
                        related_messages=[0],
                        is_duplicate=True,
                        duplicate_of_id="99999",
                    )
                ],
            )
            records = await store3.add_bugs_from_analysis(
                umo="test",
                analysis_result=result,
                raw_messages=[MessageRecord(0, "u1", "A", "msg")],
            )
            print(f"    ✅ 重复合并（id 不存在）→ 新建 {len(records)} 条记录")
            assert len(records) == 1

    # ------------------------------------------------------------------
    # 5. DashboardAPI — 参数边界与响应兜底
    # ------------------------------------------------------------------

    def test_dashboard_param_boundaries(self):
        """Dashboard API 参数边界。"""
        print("\n  [DashboardAPI] 参数边界与响应兜底:")

        class FakeArgs:
            def __init__(self, data):
                self._data = data

            def get(self, key, default=None):
                return self._data.get(key, default)

        cases = [
            ({"page": "abc", "page_size": "xyz"}, 1, 20, "非法字符串"),
            ({"page": "-5", "page_size": "0"}, 1, 1, "负数/零"),
            ({"page": "1", "page_size": "9999"}, 1, 100, "超大 page_size"),
            ({"page": "3", "page_size": "50"}, 3, 50, "正常值"),
            ({}, 1, 20, "全缺失"),
        ]

        for args_dict, expected_page, expected_ps, desc in cases:
            args = FakeArgs(args_dict)
            page = _int_param(args, "page", 1, min_val=1)
            page_size = _int_param(args, "page_size", 20, min_val=1, max_val=100)
            print(f"    ✅ {desc:15s} → page={page}, page_size={page_size}")
            assert page == expected_page
            assert page_size == expected_ps

    # ------------------------------------------------------------------
    # 6. ChatBuffer — 并发入队 + TTL 清理竞态
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_concurrent_add_and_ttl_race(self):
        """并发入队安全 + TTL 清理 TOCTOU 防护。"""
        print("\n  [ChatBuffer] 并发入队 + TTL 清理竞态:")

        mgr = ChatBufferManager(
            {
                "batch_size": 200,
                "max_history": 50,
                "time_threshold_min": 30,
                "analysis_interval_min": 5,
            }
        )

        async def spam(umo: str, n: int):
            for i in range(n):
                await mgr.add_message(umo, "u1", "用户", f"msg {i}")

        await asyncio.gather(
            spam("umo_a", 100),
            spam("umo_a", 100),
            spam("umo_b", 50),
        )
        print(
            f"    ✅ umo_a 并发 200 条 → 实际缓存 {mgr.get_buffer_size('umo_a')} 条（maxlen=50）"
        )
        print(f"    ✅ umo_b 并发 50 条 → 实际缓存 {mgr.get_buffer_size('umo_b')} 条")
        assert mgr.get_buffer_size("umo_a") == 50
        assert mgr.get_buffer_size("umo_b") == 50

        # 模拟 TTL 清理：伪造过期 UMO
        await mgr.add_message("expired_umo", "u1", "用户", "msg")
        mgr._last_active["expired_umo"] = time.time() - 25 * 3600
        await mgr._do_cleanup()
        print(
            f"    ✅ 过期 UMO 清理 → {mgr.get_buffer_size('expired_umo')} 条（应为 0）"
        )
        assert mgr.get_buffer_size("expired_umo") == 0
