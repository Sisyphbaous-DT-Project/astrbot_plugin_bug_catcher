"""
Dashboard 后端 API。

封装所有 Dashboard 需要的 Web API handler，供前端 Bridge SDK 调用。
"""

from __future__ import annotations

from typing import Any

from quart import jsonify, request

from astrbot.api import logger

from .bug_store import BugStore
from .diagnostics import DiagnosticsStore


class DashboardAPI:
    """Dashboard 后端 API 封装。"""

    def __init__(
        self, bug_store: BugStore, diagnostics: DiagnosticsStore | None = None
    ):
        self.bug_store = bug_store
        self.diagnostics = diagnostics or DiagnosticsStore()
        self.prefix = "/astrbot_plugin_bug_catcher"
        self._registered: list[tuple[str, list[str]]] = []

    # ------------------------------------------------------------------
    # 注册到 AstrBot
    # ------------------------------------------------------------------

    def register(self, context) -> None:
        """将所有 API 注册到 AstrBot Web 路由系统。"""
        apis = [
            (f"{self.prefix}/bugs", self.get_bugs, ["GET"]),
            (f"{self.prefix}/bugs/<id>/delete", self.delete_bug, ["POST"]),
            (f"{self.prefix}/bugs/<id>/status", self.update_status, ["POST"]),
            (f"{self.prefix}/stats", self.get_stats, ["GET"]),
            (
                f"{self.prefix}/diagnostics/summary",
                self.get_diagnostics_summary,
                ["GET"],
            ),
            (f"{self.prefix}/diagnostics", self.get_diagnostics, ["GET"]),
            (f"{self.prefix}/diagnostics/read", self.mark_diagnostics_read, ["POST"]),
            (f"{self.prefix}/diagnostics/clear", self.clear_diagnostics, ["POST"]),
        ]
        for route, handler, methods in apis:
            context.register_web_api(
                route=route,
                view_handler=handler,
                methods=methods,
                desc=handler.__doc__ or "",
            )
            self._registered.append((route, methods))
            logger.info(f"[DashboardAPI] 注册路由: {route} [{','.join(methods)}]")

    def unregister(self, context) -> None:
        """从 AstrBot Web 路由系统中注销本插件的所有 API。"""
        apis = getattr(context, "registered_web_apis", None)
        if apis is None:
            return
        for route, methods in self._registered:
            context.registered_web_apis[:] = [
                api
                for api in context.registered_web_apis
                if not (api[0] == route and api[2] == methods)
            ]
            logger.info(f"[DashboardAPI] 注销路由: {route} [{','.join(methods)}]")
        self._registered.clear()

    # ------------------------------------------------------------------
    # 响应辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(data: Any = None, message: str = "success") -> Any:
        """成功响应。"""
        payload = {"code": 0, "message": message}
        if data is not None:
            payload["data"] = data
        return jsonify(payload)

    @staticmethod
    def _error(message: str, code: int = 1) -> Any:
        """错误响应。"""
        return jsonify({"code": code, "message": message})

    # ------------------------------------------------------------------
    # Handler 实现
    # ------------------------------------------------------------------

    async def get_bugs(self, **kwargs) -> Any:
        """获取 bug 列表（支持分页、过滤）。"""
        # 解析查询参数
        page = _int_param(request.args, "page", 1, min_val=1)
        page_size = _int_param(request.args, "page_size", 20, min_val=1, max_val=100)
        result = request.args.get("result") or None
        severity = request.args.get("severity") or None
        status = request.args.get("status") or None
        umo = request.args.get("umo") or None
        sort_by = request.args.get("sort_by", "created_at")
        if sort_by not in ("created_at", "severity"):
            sort_by = "created_at"
        sort_desc = _bool_param(request.args, "sort_desc", True)

        try:
            bugs, total = await self.bug_store.get_bugs(
                result=result,
                severity=severity,
                status=status,
                umo=umo,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_desc=sort_desc,
            )
        except Exception as e:
            logger.error(f"[DashboardAPI] 查询 bug 列表失败: {e}")
            await self.diagnostics.record_error(
                "Dashboard 查询 bug 列表失败",
                e,
                source="dashboard.get_bugs",
            )
            return self._error(str(e))

        return self._ok(
            {
                "bugs": [bug.to_dict() for bug in bugs],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    async def delete_bug(self, **kwargs) -> Any:
        """删除指定 bug。"""
        bug_id = kwargs.get("id")
        if not bug_id:
            return self._error("缺少 bug id")

        try:
            success = await self.bug_store.delete_bug(bug_id)
        except Exception as e:
            logger.error(f"[DashboardAPI] 删除 bug 失败: {e}")
            await self.diagnostics.record_error(
                "Dashboard 删除 bug 失败",
                e,
                source="dashboard.delete_bug",
                context={"bug_id": bug_id},
            )
            return self._error(str(e))

        if not success:
            return self._error("记录不存在", code=404)
        return self._ok(message="删除成功")

    async def update_status(self, **kwargs) -> Any:
        """更新 bug 状态。"""
        bug_id = kwargs.get("id")
        if not bug_id:
            return self._error("缺少 bug id")

        try:
            body = await request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}

        status = body.get("status")
        note = body.get("note", "")

        if not status:
            return self._error("缺少 status 字段")

        try:
            success = await self.bug_store.update_bug_status(bug_id, status, note)
        except Exception as e:
            logger.error(f"[DashboardAPI] 更新状态失败: {e}")
            await self.diagnostics.record_error(
                "Dashboard 更新状态失败",
                e,
                source="dashboard.update_status",
                context={"bug_id": bug_id, "status": status},
            )
            return self._error(str(e))

        if not success:
            return self._error("记录不存在或状态无效", code=400)
        return self._ok(message="更新成功")

    async def get_stats(self, **kwargs) -> Any:
        """获取统计信息。"""
        try:
            stats = await self.bug_store.get_stats()
        except Exception as e:
            logger.error(f"[DashboardAPI] 获取统计失败: {e}")
            await self.diagnostics.record_error(
                "Dashboard 获取统计失败",
                e,
                source="dashboard.get_stats",
            )
            return self._error(str(e))

        return self._ok(stats)

    async def get_diagnostics_summary(self, **kwargs) -> Any:
        """获取插件诊断状态摘要。"""
        try:
            summary = await self.diagnostics.get_summary()
        except Exception as e:
            logger.error(f"[DashboardAPI] 获取诊断摘要失败: {e}")
            return self._error(str(e))
        return self._ok(summary)

    async def get_diagnostics(self, **kwargs) -> Any:
        """获取插件诊断事件列表。"""
        limit = _int_param(request.args, "limit", 20, min_val=1, max_val=100)
        unread_only = _bool_param(request.args, "unread_only", False)
        try:
            events = await self.diagnostics.list_events(
                limit=limit,
                unread_only=unread_only,
            )
        except Exception as e:
            logger.error(f"[DashboardAPI] 获取诊断事件失败: {e}")
            return self._error(str(e))
        return self._ok({"events": events})

    async def mark_diagnostics_read(self, **kwargs) -> Any:
        """标记插件诊断事件为已读。"""
        try:
            body = await request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}
        ids = body.get("ids")
        if ids is not None and not isinstance(ids, list):
            return self._error("ids 必须是列表")
        count, saved = await self.diagnostics.mark_read(ids=ids)
        if not saved:
            return self._error("诊断已读状态保存失败")
        return self._ok({"marked": count}, message="已标记为已读")

    async def clear_diagnostics(self, **kwargs) -> Any:
        """清空插件诊断事件。"""
        count, saved = await self.diagnostics.clear()
        if not saved:
            return self._error("诊断记录清空失败")
        return self._ok({"cleared": count}, message="诊断记录已清空")


# ------------------------------------------------------------------
# 参数解析辅助
# ------------------------------------------------------------------


def _int_param(
    args, key: str, default: int, min_val: int = 1, max_val: int | None = None
) -> int:
    """从查询参数解析整数（自动 clamp 到 [min_val, max_val]）。"""
    try:
        val = int(args.get(key, default))
        val = max(min_val, val)
        if max_val is not None:
            val = min(max_val, val)
        return val
    except (ValueError, TypeError):
        return default


def _bool_param(args, key: str, default: bool) -> bool:
    """从查询参数解析布尔值。"""
    val = args.get(key, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default
