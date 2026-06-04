"""
AstrBot Bug Catcher Plugin

自动监听群聊消息，利用 AI 识别 bug 反馈并记录到 Dashboard。
"""

import logging
import asyncio

from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.event import AstrMessageEvent

from .chat_buffer import ChatBufferManager
from .analyzer import BugAnalyzer, AnalysisResult
from .bug_store import BugStore
from .dashboard_api import DashboardAPI


class BugCatcherPlugin(Star):
    """Bug Catcher 插件主类。"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)
        self.config = config or {}
        self.buffer_mgr = ChatBufferManager(self.config)
        self.analyzer = BugAnalyzer(self.context, self.config)
        self.bug_store = BugStore()
        self.dashboard_api = DashboardAPI(self.bug_store)
        self._active = True
        self._analysis_tasks: set[asyncio.Task] = set()
        self._scan_task: asyncio.Task | None = None
        self.dashboard_api.register(self.context)
        logger.info(f"[BugCatcher] 插件初始化完成，配置: {self.config}")

    async def initialize(self):
        """插件激活后调用，完成异步初始化。"""
        self._active = True
        await self.bug_store.load()
        self.buffer_mgr.start_cleanup_task()
        self._start_scan_task()
        logger.info("[BugCatcher] 插件已激活，消息缓存系统已启动")

    async def terminate(self):
        """插件禁用时调用，释放资源。"""
        self._active = False
        self.buffer_mgr.stop_cleanup_task()
        await self._stop_scan_task()
        await self._cancel_analysis_tasks()
        self.dashboard_api.unregister(self.context)
        logger.info("[BugCatcher] 插件已停用，资源已释放")

    # ------------------------------------------------------------------
    # 白名单 / 全局开关检查
    # ------------------------------------------------------------------

    def _should_process(self, umo: str) -> bool:
        """检查该 UMO 是否应被处理。"""
        if self.config.get("global_mode", False):
            return True
        whitelist = self.config.get("umo_whitelist", [])
        return umo in whitelist

    # ------------------------------------------------------------------
    # 群聊消息处理器
    # ------------------------------------------------------------------

    @event_message_type(EventMessageType.GROUP_MESSAGE, priority=0)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息，过滤后入队，满足条件时触发分析。"""
        try:
            umo = event.unified_msg_origin

            if not self._active:
                return

            # 白名单 / 全局开关检查
            if not self._should_process(umo):
                return

            # 提取消息信息
            sender_id_raw = event.get_sender_id()
            sender_name_raw = event.get_sender_name()
            content_raw = event.get_message_outline()
            sender_id = "" if sender_id_raw is None else str(sender_id_raw)
            sender_name = "未知" if sender_name_raw is None else str(sender_name_raw)
            content = "" if content_raw is None else str(content_raw)

            logger.debug(
                f"[BugCatcher] 收到群聊消息 from {sender_name}({sender_id}): "
                f"{content[:80]}{'...' if len(content) > 80 else ''}"
            )

            # 消息入队并检查触发条件
            trigger = await self.buffer_mgr.add_message(
                umo=umo,
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
            )

            if trigger.triggered:
                logger.info(
                    f"[BugCatcher] UMO={umo} 触发分析，"
                    f"原因: {trigger.reason}, 消息数: {len(trigger.messages)}"
                )
                if not self._schedule_analysis(umo, trigger.messages):
                    await self.buffer_mgr.mark_analysis_complete(umo)
        except Exception as e:
            # 顶层保护：任何异常都不应阻断事件传播
            logger.error(f"[BugCatcher] 处理群聊消息异常: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 分析任务管理
    # ------------------------------------------------------------------

    def _schedule_analysis(self, umo: str, messages: list) -> bool:
        """创建受插件生命周期管理的分析任务。"""
        if not self._active:
            return False
        task = asyncio.create_task(
            self._analyze_and_store(umo, messages),
            name=f"bug_catcher_analysis:{umo}",
        )
        self._analysis_tasks.add(task)

        def _done_callback(done_task: asyncio.Task) -> None:
            self._analysis_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                done_task.result()
            except Exception as e:
                logger.error(f"[BugCatcher] 分析任务异常: {e}", exc_info=True)

        task.add_done_callback(_done_callback)
        return True

    async def _cancel_analysis_tasks(self) -> None:
        """取消所有仍在执行的分析任务。"""
        if not self._analysis_tasks:
            return
        tasks = list(self._analysis_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._analysis_tasks.clear()

    def _start_scan_task(self) -> None:
        """启动低活跃缓冲区主动扫描任务。"""
        if self._scan_task is not None and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(
            self._scan_due_buffers_loop(),
            name="bug_catcher_due_scan",
        )

    async def _stop_scan_task(self) -> None:
        """停止低活跃缓冲区主动扫描任务。"""
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            await asyncio.gather(self._scan_task, return_exceptions=True)
        self._scan_task = None

    async def _scan_due_buffers_loop(self) -> None:
        """定期触发超过时间阈值的低活跃缓冲区分析。"""
        while self._active:
            try:
                await asyncio.sleep(60)
                await self._scan_due_buffers_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[BugCatcher] 时间阈值扫描异常: {e}", exc_info=True)

    async def _scan_due_buffers_once(self) -> None:
        """扫描一次所有满足触发条件的缓冲区。"""
        if not self._active:
            return
        triggers = await self.buffer_mgr.collect_due_triggers()
        for umo, trigger in triggers:
            logger.info(
                f"[BugCatcher] UMO={umo} 主动触发分析，"
                f"原因: {trigger.reason}, 消息数: {len(trigger.messages)}"
            )
            if not self._schedule_analysis(umo, trigger.messages):
                await self.buffer_mgr.mark_analysis_complete(umo)

    # ------------------------------------------------------------------
    # 分析引擎入口
    # ------------------------------------------------------------------

    async def _analyze_and_store(self, umo: str, messages: list) -> None:
        """调用 AI 分析并保存结果。"""
        logger.info(f"[BugCatcher] 开始分析 UMO={umo}，消息数: {len(messages)}")

        try:
            # 获取已有 bug 列表（open 状态），供 AI 去重参考
            try:
                existing_bugs = await self.bug_store.get_open_bugs(limit=30)
            except Exception as e:
                logger.warning(f"[BugCatcher] 获取已有 bug 列表失败: {e}")
                existing_bugs = []

            try:
                result: AnalysisResult = await self.analyzer.analyze(
                    messages, umo, existing_bugs=existing_bugs
                )
            except Exception as e:
                logger.error(f"[BugCatcher] 分析异常: {e}", exc_info=True)
                return

            if result.error:
                logger.warning(f"[BugCatcher] 分析失败: {result.error}")
                return

            if result.result == "none":
                logger.info("[BugCatcher] 分析结果: 未发现 bug")
                return

            if not self._active:
                logger.info("[BugCatcher] 插件已停用，跳过保存分析结果")
                return

            log_level = (
                logging.WARNING if result.result == "suspected" else logging.INFO
            )
            for bug in result.bugs:
                logger.log(
                    log_level,
                    "[BugCatcher] 发现 %s bug: [%s] %s",
                    result.result,
                    bug.severity,
                    bug.summary,
                )

            # 保存到 BugStore
            try:
                platform = umo.split(":", 1)[0] if ":" in umo else ""
                # 尝试获取主报告者信息（取相关消息中的第一个发送者）
                reporter_name = ""
                reporter_id = ""
                if messages:
                    reporter_name = messages[0].sender_name
                    reporter_id = messages[0].sender_id

                records = await self.bug_store.add_bugs_from_analysis(
                    umo=umo,
                    analysis_result=result,
                    raw_messages=messages,
                    platform=platform,
                    reporter_name=reporter_name,
                    reporter_id=reporter_id,
                )
                logger.info(f"[BugCatcher] 已保存 {len(records)} 条记录到 BugStore")
            except Exception as e:
                logger.error(f"[BugCatcher] 保存 bug 记录失败: {e}", exc_info=True)
        finally:
            await self.buffer_mgr.mark_analysis_complete(umo)
