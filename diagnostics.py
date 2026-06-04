"""
插件自身诊断日志。

仅记录 Bug Catcher 插件运行过程中的 warning/error，用于 Dashboard 展示插件健康状态。
不记录群聊原文、Prompt 或模型判断详情。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class DiagnosticsStore:
    """插件诊断事件的轻量 JSON 存储。"""

    _VERSION = 1
    _FILE_NAME = "diagnostics.json"
    _VALID_LEVELS = {"warning", "error"}

    def __init__(self, config: dict | None = None, data_dir: str | None = None):
        config = config or {}
        if data_dir is None:
            data_dir = os.path.join(get_astrbot_data_path(), "plugin_bug_catcher")
        os.makedirs(data_dir, exist_ok=True)
        self._file_path = os.path.join(data_dir, self._FILE_NAME)
        self._lock = asyncio.Lock()
        self._events: list[dict[str, Any]] = []
        self.max_entries = max(20, int(config.get("diagnostics_max_entries", 200)))

    async def load(self) -> None:
        """加载历史诊断事件。"""
        if not os.path.exists(self._file_path):
            return

        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[Diagnostics] 加载诊断日志失败: {e}")
            return

        raw_events = data.get("events", []) if isinstance(data, dict) else []
        if not isinstance(raw_events, list):
            logger.error("[Diagnostics] 诊断日志格式错误，期望 events 为 list")
            return

        self._events = [event for event in raw_events if isinstance(event, dict)]
        self._trim()

    async def record_warning(
        self,
        title: str,
        message: str,
        *,
        source: str = "runtime",
        context: dict[str, Any] | None = None,
    ) -> None:
        """记录 warning 级别诊断。"""
        await self.record_event(
            "warning",
            title,
            message,
            source=source,
            context=context,
        )

    async def record_error(
        self,
        title: str,
        error: BaseException | str,
        *,
        source: str = "runtime",
        context: dict[str, Any] | None = None,
        include_traceback: bool = True,
    ) -> None:
        """记录 error 级别诊断。"""
        if isinstance(error, BaseException):
            message = str(error) or error.__class__.__name__
        else:
            message = str(error)
        safe_context = dict(context or {})
        if isinstance(error, BaseException) and include_traceback:
            safe_context["traceback"] = traceback.format_exception_only(
                type(error),
                error,
            )[-1].strip()
        await self.record_event(
            "error",
            title,
            message,
            source=source,
            context=safe_context,
        )

    async def record_event(
        self,
        level: str,
        title: str,
        message: str,
        *,
        source: str = "runtime",
        context: dict[str, Any] | None = None,
    ) -> None:
        """记录一条诊断事件。"""
        level = level.lower()
        if level not in self._VALID_LEVELS:
            level = "warning"

        event = {
            "id": str(uuid.uuid4()),
            "level": level,
            "title": _safe_text(title, 80),
            "message": _safe_text(message, 500),
            "source": _safe_text(source, 80),
            "context": _sanitize_context(context or {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "timestamp": time.time(),
            "unread": True,
        }

        async with self._lock:
            self._events.append(event)
            self._trim()
            await self._save_locked()

    async def list_events(
        self,
        *,
        limit: int = 20,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        """返回最近诊断事件。"""
        limit = max(1, min(int(limit), 100))
        async with self._lock:
            events = list(reversed(self._events))
            if unread_only:
                events = [event for event in events if event.get("unread")]
            return [dict(event) for event in events[:limit]]

    async def get_summary(self) -> dict[str, Any]:
        """返回红点所需的聚合状态。"""
        async with self._lock:
            unread_errors = [
                event
                for event in self._events
                if event.get("unread") and event.get("level") == "error"
            ]
            unread_warnings = [
                event
                for event in self._events
                if event.get("unread") and event.get("level") == "warning"
            ]
            latest_error = next(
                (
                    event
                    for event in reversed(self._events)
                    if event.get("level") == "error"
                ),
                None,
            )
            status = (
                "error" if unread_errors else "warning" if unread_warnings else "ok"
            )
            return {
                "status": status,
                "unread_error_count": len(unread_errors),
                "unread_warning_count": len(unread_warnings),
                "unread_count": len(unread_errors) + len(unread_warnings),
                "latest_error_at": latest_error.get("created_at")
                if latest_error
                else "",
                "total": len(self._events),
            }

    async def mark_read(self, ids: list[str] | None = None) -> int:
        """标记诊断事件为已读；ids 为空时标记全部。"""
        id_set = set(ids or [])
        count = 0
        async with self._lock:
            for event in self._events:
                if ids and event.get("id") not in id_set:
                    continue
                if event.get("unread"):
                    event["unread"] = False
                    count += 1
            if count:
                await self._save_locked()
        return count

    async def clear(self) -> int:
        """清空全部诊断事件。"""
        async with self._lock:
            count = len(self._events)
            self._events = []
            await self._save_locked()
        return count

    def _trim(self) -> None:
        if len(self._events) > self.max_entries:
            self._events = self._events[-self.max_entries :]

    async def _save_locked(self) -> None:
        data = {
            "version": self._VERSION,
            "events": self._events,
        }
        tmp_path = self._file_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
        except OSError as e:
            logger.error(f"[Diagnostics] 保存诊断日志失败: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass


def _safe_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _sanitize_context(context: dict[str, Any]) -> dict[str, str | int | float | bool]:
    safe: dict[str, str | int | float | bool] = {}
    for key, value in context.items():
        safe_key = _safe_text(key, 60)
        if isinstance(value, (bool, int, float)):
            safe[safe_key] = value
        else:
            safe[safe_key] = _safe_text(value, 240)
    return safe
