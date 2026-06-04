"""
Dashboard 后端 API。

封装所有 Dashboard 需要的 Web API handler，供前端 Bridge SDK 调用。
"""

from __future__ import annotations

from typing import Any, Optional

from quart import jsonify, request

from astrbot.api import logger

from .bug_store import BugStore


class DashboardAPI:
    """Dashboard 后端 API 封装。"""

    def __init__(self, bug_store: BugStore):
        self.bug_store = bug_store
        self.prefix = "/astrbot_plugin_bug_catcher"

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
        ]
        for route, handler, methods in apis:
            context.register_web_api(
                route=route,
                view_handler=handler,
                methods=methods,
                desc=handler.__doc__ or "",
            )
            logger.info(f"[DashboardAPI] 注册路由: {route} [{','.join(methods)}]")

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
        page_size = _int_param(request.args, "page_size", 20, min_val=1)
        result = request.args.get("result") or None
        severity = request.args.get("severity") or None
        status = request.args.get("status") or None
        umo = request.args.get("umo") or None
        sort_by = request.args.get("sort_by", "created_at")
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
            success = await self.bug_store.update_bug_status(
                bug_id, status, note
            )
        except Exception as e:
            logger.error(f"[DashboardAPI] 更新状态失败: {e}")
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
            return self._error(str(e))

        return self._ok(stats)


# ------------------------------------------------------------------
# 参数解析辅助
# ------------------------------------------------------------------

def _int_param(args, key: str, default: int, min_val: int = 1) -> int:
    """从查询参数解析整数（自动 clamp 到 min_val）。"""
    try:
        val = int(args.get(key, default))
        return max(min_val, val)
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
