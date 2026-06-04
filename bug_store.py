"""
Bug 持久化存储。

JSON 文件读写，支持 CRUD、分页查询、状态更新。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .chat_buffer import MessageRecord
from .analyzer import AnalysisResult


@dataclass
class BugRecord:
    """单条 Bug 记录。"""

    id: str
    umo: str
    umo_display: str
    platform: str
    created_at: str
    result: str  # suspected | confirmed
    severity: str
    summary: str
    analysis: str
    related_messages: List[int]
    raw_messages: List[dict]  # MessageRecord 的 dict 形式
    status: str = "open"  # open | resolved | ignored
    resolved_at: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        """转换为可序列化的 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BugRecord":
        """从 dict 还原。"""
        # 过滤掉类中不存在的字段（向前兼容）
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class BugStore:
    """Bug 记录的 JSON 持久化存储。"""

    _VERSION = 1
    _FILE_NAME = "bugs.json"

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            data_dir = os.path.join(
                get_astrbot_data_path(), "plugin_bug_catcher"
            )
        os.makedirs(data_dir, exist_ok=True)
        self._file_path = os.path.join(data_dir, self._FILE_NAME)
        self._lock = asyncio.Lock()
        self._bugs: Dict[str, BugRecord] = {}
        self._stats = {
            "total_confirmed": 0,
            "total_suspected": 0,
            "total_analyzed": 0,
        }

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """从 JSON 文件加载已有数据。"""
        if not os.path.exists(self._file_path):
            logger.info("[BugStore] 数据文件不存在，初始化空存储")
            return

        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[BugStore] 加载数据失败: {e}")
            return

        if not isinstance(data, dict):
            logger.error("[BugStore] 数据文件格式错误，期望 dict")
            return

        version = data.get("version", 1)
        if version != self._VERSION:
            logger.warning(
                f"[BugStore] 数据版本 {version} 与当前版本 {self._VERSION} 不匹配"
            )

        bugs_data = data.get("bugs", [])
        if not isinstance(bugs_data, list):
            logger.error("[BugStore] bugs 字段格式错误，期望 list")
            bugs_data = []

        self._bugs = {}
        for bug_data in bugs_data:
            try:
                bug = BugRecord.from_dict(bug_data)
                self._bugs[bug.id] = bug
            except Exception as e:
                logger.warning(f"[BugStore] 跳过损坏的记录: {e}")

        self._stats = data.get("stats", self._stats)
        if not isinstance(self._stats, dict):
            self._stats = {
                "total_confirmed": 0,
                "total_suspected": 0,
                "total_analyzed": 0,
            }
        logger.info(f"[BugStore] 已加载 {len(self._bugs)} 条记录")

    async def _save(self) -> None:
        """异步保存到 JSON 文件。"""
        data = {
            "version": self._VERSION,
            "bugs": [bug.to_dict() for bug in self._bugs.values()],
            "stats": self._stats,
        }
        # 使用临时文件 + 原子重命名，避免写入中断导致文件损坏
        tmp_path = self._file_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._file_path)
        except OSError as e:
            logger.error(f"[BugStore] 保存数据失败: {e}")
            raise

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_bugs_from_analysis(
        self,
        umo: str,
        analysis_result: AnalysisResult,
        raw_messages: List[MessageRecord],
        platform: str = "",
    ) -> List[BugRecord]:
        """将分析结果中的 bug 添加到存储。"""
        if analysis_result.result == "none" or not analysis_result.bugs:
            return []

        async with self._lock:
            records = []
            now = datetime.now(timezone.utc).isoformat()
            for bug_item in analysis_result.bugs:
                record = BugRecord(
                    id=str(uuid.uuid4()),
                    umo=umo,
                    umo_display=self._build_umo_display(umo),
                    platform=platform,
                    created_at=now,
                    result=analysis_result.result,
                    severity=bug_item.severity,
                    summary=bug_item.summary,
                    analysis=bug_item.analysis,
                    related_messages=bug_item.related_messages,
                    raw_messages=[
                        {
                            "timestamp": m.timestamp,
                            "sender_name": m.sender_name,
                            "content": m.content,
                        }
                        for m in raw_messages
                    ],
                )
                self._bugs[record.id] = record
                records.append(record)

            # 更新统计
            if analysis_result.result == "confirmed":
                self._stats["total_confirmed"] += len(records)
            else:
                self._stats["total_suspected"] += len(records)
            self._stats["total_analyzed"] += 1

            await self._save()
            logger.info(
                f"[BugStore] 新增 {len(records)} 条 {analysis_result.result} 记录"
            )
            return records

    async def get_bugs(
        self,
        result: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        umo: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_desc: bool = True,
    ) -> tuple[List[BugRecord], int]:
        """分页查询 bug 记录。

        Returns:
            (记录列表, 总记录数)
        """
        async with self._lock:
            bugs = list(self._bugs.values())

        # 过滤
        if result:
            bugs = [b for b in bugs if b.result == result]
        if severity:
            bugs = [b for b in bugs if b.severity == severity]
        if status:
            bugs = [b for b in bugs if b.status == status]
        if umo:
            bugs = [b for b in bugs if b.umo == umo]

        # 排序
        reverse = sort_desc
        if sort_by == "created_at":
            bugs.sort(key=lambda b: b.created_at, reverse=reverse)
        elif sort_by == "severity":
            order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            bugs.sort(key=lambda b: order.get(b.severity, 99), reverse=not reverse)

        total = len(bugs)
        start = (page - 1) * page_size
        end = start + page_size
        return bugs[start:end], total

    async def get_bug_by_id(self, bug_id: str) -> Optional[BugRecord]:
        """按 ID 查询单条记录。"""
        async with self._lock:
            return self._bugs.get(bug_id)

    async def update_bug_status(
        self, bug_id: str, status: str, note: str = ""
    ) -> bool:
        """更新 bug 状态。

        Returns:
            是否更新成功
        """
        if status not in ("open", "resolved", "ignored"):
            logger.warning(f"[BugStore] 无效的状态值: {status}")
            return False

        async with self._lock:
            bug = self._bugs.get(bug_id)
            if not bug:
                return False
            bug.status = status
            if note:
                bug.note = note
            if status == "resolved":
                bug.resolved_at = datetime.now(timezone.utc).isoformat()
            await self._save()
            logger.info(f"[BugStore] 更新状态: {bug_id} -> {status}")
            return True

    async def delete_bug(self, bug_id: str) -> bool:
        """删除 bug 记录。"""
        async with self._lock:
            if bug_id not in self._bugs:
                return False
            del self._bugs[bug_id]
            await self._save()
            logger.info(f"[BugStore] 删除记录: {bug_id}")
            return True

    async def get_stats(self) -> dict:
        """获取统计信息。"""
        async with self._lock:
            return dict(self._stats)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _build_umo_display(umo: str) -> str:
        """从 UMO 构建可读显示名称。

        UMO 格式: platform:type:session_id
        """
        parts = umo.split(":", 2)
        if len(parts) >= 3:
            return f"{parts[0]}: {parts[2]}"
        return umo
