"""
DashboardAPI 单元测试。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot_plugin_bug_catcher.dashboard_api import DashboardAPI
from astrbot_plugin_bug_catcher.bug_store import BugStore, BugRecord


@pytest.mark.unit
class TestDashboardAPI:
    """测试 Dashboard 后端 API。"""

    @pytest.fixture
    def mock_bug_store(self):
        store = MagicMock(spec=BugStore)
        store.get_bugs = AsyncMock(return_value=([], 0))
        store.get_stats = AsyncMock(return_value={})
        store.delete_bug = AsyncMock(return_value=True)
        store.update_bug_status = AsyncMock(return_value=True)
        store.get_bug_by_id = AsyncMock(return_value=None)
        return store

    @pytest.fixture
    def api(self, mock_bug_store):
        return DashboardAPI(mock_bug_store)

    @pytest.fixture
    def mock_context(self):
        ctx = MagicMock()
        ctx.register_web_api = MagicMock()
        return ctx

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def test_register_routes(self, api, mock_context):
        """应注册 4 个路由。"""
        api.register(mock_context)
        assert mock_context.register_web_api.call_count == 4

    # ------------------------------------------------------------------
    # get_bugs
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_bugs_default(self, api, mock_bug_store):
        """默认查询应使用正确参数。"""
        mock_bug_store.get_bugs.return_value = (
            [
                BugRecord(
                    id="test-1",
                    umo="test",
                    umo_display="test",
                    platform="aiocqhttp",
                    created_at="2026-06-04T00:00:00+00:00",
                    result="confirmed",
                    severity="high",
                    summary="test bug",
                    analysis="test analysis",
                    related_messages=[0],
                    raw_messages=[],
                )
            ],
            1,
        )

        with patch("astrbot_plugin_bug_catcher.dashboard_api.request") as mock_req:
            mock_req.args = {}
            resp = await api.get_bugs()

        assert resp.status_code == 200
        json_data = await resp.get_json()
        assert json_data["code"] == 0
        assert json_data["data"]["total"] == 1
        assert len(json_data["data"]["bugs"]) == 1

    @pytest.mark.asyncio
    async def test_get_bugs_with_filters(self, api, mock_bug_store):
        """带筛选参数的查询应正确传递。"""
        with patch("astrbot_plugin_bug_catcher.dashboard_api.request") as mock_req:
            mock_req.args = {
                "severity": "high",
                "status": "open",
                "result": "confirmed",
                "page": "2",
                "page_size": "10",
            }
            await api.get_bugs()

        call_kwargs = mock_bug_store.get_bugs.call_args.kwargs
        assert call_kwargs["severity"] == "high"
        assert call_kwargs["status"] == "open"
        assert call_kwargs["result"] == "confirmed"
        assert call_kwargs["page"] == 2
        assert call_kwargs["page_size"] == 10

    @pytest.mark.asyncio
    async def test_get_bugs_error(self, api, mock_bug_store):
        """查询异常应返回错误。"""
        mock_bug_store.get_bugs.side_effect = RuntimeError("DB error")

        with patch("astrbot_plugin_bug_catcher.dashboard_api.request") as mock_req:
            mock_req.args = {}
            resp = await api.get_bugs()

        json_data = await resp.get_json()
        assert json_data["code"] == 1
        assert "DB error" in json_data["message"]

    # ------------------------------------------------------------------
    # delete_bug
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_bug_success(self, api, mock_bug_store):
        """删除成功应返回成功响应。"""
        with patch("astrbot_plugin_bug_catcher.dashboard_api.request"):
            resp = await api.delete_bug(id="bug-123")

        json_data = await resp.get_json()
        assert json_data["code"] == 0
        assert "删除成功" in json_data["message"]

    @pytest.mark.asyncio
    async def test_delete_bug_not_found(self, api, mock_bug_store):
        """删除不存在的记录应返回 404。"""
        mock_bug_store.delete_bug.return_value = False

        with patch("astrbot_plugin_bug_catcher.dashboard_api.request"):
            resp = await api.delete_bug(id="missing")

        json_data = await resp.get_json()
        assert json_data["code"] == 404

    @pytest.mark.asyncio
    async def test_delete_bug_no_id(self, api):
        """缺少 ID 应返回错误。"""
        with patch("astrbot_plugin_bug_catcher.dashboard_api.request"):
            resp = await api.delete_bug()

        json_data = await resp.get_json()
        assert json_data["code"] == 1
        assert "缺少" in json_data["message"]

    # ------------------------------------------------------------------
    # update_status
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_update_status_success(self, api, mock_bug_store):
        """更新状态成功。"""
        with patch(
            "astrbot_plugin_bug_catcher.dashboard_api.request"
        ) as mock_req:
            mock_req.get_json = AsyncMock(return_value={"status": "resolved"})
            resp = await api.update_status(id="bug-123")

        json_data = await resp.get_json()
        assert json_data["code"] == 0

    @pytest.mark.asyncio
    async def test_update_status_invalid(self, api, mock_bug_store):
        """更新无效状态应失败。"""
        mock_bug_store.update_bug_status.return_value = False

        with patch(
            "astrbot_plugin_bug_catcher.dashboard_api.request"
        ) as mock_req:
            mock_req.get_json = AsyncMock(return_value={"status": "resolved"})
            resp = await api.update_status(id="bug-123")

        json_data = await resp.get_json()
        assert json_data["code"] == 400

    @pytest.mark.asyncio
    async def test_update_status_missing_body(self, api):
        """缺少请求体应返回错误。"""
        with patch(
            "astrbot_plugin_bug_catcher.dashboard_api.request"
        ) as mock_req:
            mock_req.get_json = AsyncMock(return_value={})
            resp = await api.update_status(id="bug-123")

        json_data = await resp.get_json()
        assert json_data["code"] == 1
        assert "缺少 status" in json_data["message"]

    # ------------------------------------------------------------------
    # get_stats
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_stats(self, api, mock_bug_store):
        """应返回统计信息。"""
        mock_bug_store.get_stats.return_value = {
            "total_confirmed": 5,
            "total_suspected": 2,
        }

        with patch("astrbot_plugin_bug_catcher.dashboard_api.request"):
            resp = await api.get_stats()

        json_data = await resp.get_json()
        assert json_data["code"] == 0
        assert json_data["data"]["total_confirmed"] == 5

    @pytest.mark.asyncio
    async def test_get_stats_error(self, api, mock_bug_store):
        """统计异常应返回错误。"""
        mock_bug_store.get_stats.side_effect = RuntimeError("fail")

        with patch("astrbot_plugin_bug_catcher.dashboard_api.request"):
            resp = await api.get_stats()

        json_data = await resp.get_json()
        assert json_data["code"] == 1
