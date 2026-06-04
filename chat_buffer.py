"""
消息缓存管理器。

按 UMO 维护消息队列，FIFO 淘汰，支持批量/时间双阈值触发分析。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from astrbot.api import logger


@dataclass
class MessageRecord:
    """单条消息记录。"""

    timestamp: float
    sender_id: str
    sender_name: str
    content: str


@dataclass
class AnalysisTrigger:
    """分析触发结果。"""

    triggered: bool = False
    reason: str = ""  # "batch_size" | "time_threshold"
    messages: List[MessageRecord] = field(default_factory=list)


class ChatBufferManager:
    """按 UMO 管理消息缓存，支持 FIFO 淘汰和双阈值触发。"""

    def __init__(self, config: dict):
        # 防御性校验：确保关键配置值在合理范围内
        self.batch_size = max(1, config.get("batch_size", 200))
        self.max_history = max(10, config.get("max_history", 300))
        # 确保 batch_size 不超过 max_history，否则永远触发不了
        if self.batch_size > self.max_history:
            logger.warning(
                f"[ChatBuffer] batch_size({self.batch_size}) > max_history({self.max_history})，"
                f"自动将 batch_size 调整为 {self.max_history}"
            )
            self.batch_size = self.max_history
        self.time_threshold_min = max(1, config.get("time_threshold_min", 30))
        self.analysis_interval_min = max(1, config.get("analysis_interval_min", 5))
        self.buffer_ttl_sec = 24 * 3600  # 24 小时未活跃则清理

        # UMO -> deque[MessageRecord]
        self._buffers: Dict[str, deque[MessageRecord]] = {}
        # UMO -> 上次分析时间戳
        self._last_analysis: Dict[str, float] = {}
        # UMO -> asyncio.Lock
        self._locks: Dict[str, asyncio.Lock] = {}
        # UMO -> 最后活动时间戳（用于 TTL 清理）
        self._last_active: Dict[str, float] = {}

        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown = False

    # ------------------------------------------------------------------
    # 消息入队
    # ------------------------------------------------------------------

    async def add_message(
        self,
        umo: str,
        sender_id: str,
        sender_name: str,
        content: str,
    ) -> AnalysisTrigger:
        """添加消息到指定 UMO 的缓冲区，返回是否触发分析。"""
        lock = self._get_lock(umo)
        async with lock:
            now = time.time()
            self._last_active[umo] = now

            buf = self._buffers.setdefault(umo, deque(maxlen=self.max_history))
            buf.append(
                MessageRecord(
                    timestamp=now,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content=content,
                )
            )

            trigger = self._check_trigger(umo, now)
            if trigger.triggered:
                self._last_analysis[umo] = now
            return trigger

    # ------------------------------------------------------------------
    # 触发检查
    # ------------------------------------------------------------------

    def _check_trigger(self, umo: str, now: float) -> AnalysisTrigger:
        """检查是否满足分析触发条件。"""
        buf = self._buffers.get(umo)
        if not buf:
            return AnalysisTrigger()

        msgs = list(buf)

        # 冷却检查：任何触发条件之前先检查冷却期
        last = self._last_analysis.get(umo)
        if last is not None:
            cooldown_sec = self.analysis_interval_min * 60
            if now - last < cooldown_sec:
                return AnalysisTrigger()

        # 条件 1：消息数达到 batch_size
        if len(msgs) >= self.batch_size:
            logger.info(
                f"[ChatBuffer] UMO={umo} 触发分析: 消息数 {len(msgs)} >= "
                f"batch_size {self.batch_size}"
            )
            return AnalysisTrigger(
                triggered=True,
                reason="batch_size",
                messages=msgs,
            )

        # 条件 2：时间阈值（低活跃群聊）
        # 从未分析过的 UMO 以最早消息的 timestamp 为参考点
        reference_time = last if last is not None else msgs[0].timestamp
        time_threshold_sec = self.time_threshold_min * 60
        if now - reference_time >= time_threshold_sec and len(msgs) > 0:
            elapsed = int((now - reference_time) / 60)
            reason_text = "距离上次分析" if last is not None else "距离首条消息"
            logger.info(
                f"[ChatBuffer] UMO={umo} 触发分析: {reason_text} "
                f"{elapsed} 分钟 >= 阈值 {self.time_threshold_min} 分钟"
            )
            return AnalysisTrigger(
                triggered=True,
                reason="time_threshold",
                messages=msgs,
            )

        return AnalysisTrigger()

    # ------------------------------------------------------------------
    # 缓冲区操作
    # ------------------------------------------------------------------

    async def clear_buffer(self, umo: str) -> None:
        """清空指定 UMO 的缓冲区。

        注意：_last_analysis 必须保留以确保冷却期生效；
        _last_active 会在下次 add_message 时刷新；
        长期不活跃的 UMO 由 TTL 清理任务统一移除。
        """
        lock = self._locks.get(umo)
        if lock:
            async with lock:
                self._buffers.pop(umo, None)
        else:
            self._buffers.pop(umo, None)
        logger.debug(f"[ChatBuffer] UMO={umo} 缓冲区已清空")

    def get_buffer_size(self, umo: str) -> int:
        """获取指定 UMO 的当前缓存消息数。"""
        buf = self._buffers.get(umo)
        return len(buf) if buf else 0

    def get_all_stats(self) -> dict:
        """返回所有 UMO 的缓存统计信息。"""
        return {
            umo: {
                "count": len(buf),
                "last_analysis": self._last_analysis.get(umo, 0),
                "last_active": self._last_active.get(umo, 0),
            }
            for umo, buf in self._buffers.items()
        }

    # ------------------------------------------------------------------
    # 锁管理
    # ------------------------------------------------------------------

    def _get_lock(self, umo: str) -> asyncio.Lock:
        """获取（或创建）指定 UMO 的锁。"""
        if umo not in self._locks:
            self._locks[umo] = asyncio.Lock()
        return self._locks[umo]

    # ------------------------------------------------------------------
    # TTL 清理任务
    # ------------------------------------------------------------------

    def start_cleanup_task(self) -> None:
        """启动定期清理长期未活跃缓冲区的后台任务。"""
        if self._cleanup_task is not None:
            return
        self._shutdown = False
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="chat_buffer_cleanup",
        )
        logger.info("[ChatBuffer] TTL 清理任务已启动")

    def stop_cleanup_task(self) -> None:
        """停止清理任务。"""
        self._shutdown = True
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info("[ChatBuffer] TTL 清理任务已停止")

    async def _cleanup_loop(self) -> None:
        """每 30 分钟清理一次长期未活跃的缓冲区。"""
        try:
            while not self._shutdown:
                await asyncio.sleep(1800)  # 30 分钟
                await self._do_cleanup()
        except asyncio.CancelledError:
            logger.debug("[ChatBuffer] 清理任务被取消")
        except Exception as e:
            logger.error(f"[ChatBuffer] 清理任务异常: {e}")

    async def _do_cleanup(self) -> None:
        """执行清理。"""
        now = time.time()
        expired = [
            umo
            for umo, ts in self._last_active.items()
            if now - ts > self.buffer_ttl_sec
        ]
        for umo in expired:
            lock = self._locks.get(umo)
            if lock:
                async with lock:
                    self._buffers.pop(umo, None)
                    self._last_analysis.pop(umo, None)
                    self._last_active.pop(umo, None)
                    self._locks.pop(umo, None)
            else:
                self._buffers.pop(umo, None)
                self._last_analysis.pop(umo, None)
                self._last_active.pop(umo, None)
            logger.info(f"[ChatBuffer] UMO={umo} 缓冲区已过期清理")
