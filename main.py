"""
AstrBot Bug Catcher Plugin

自动监听群聊消息，利用 AI 识别 bug 反馈并记录到 Dashboard。
"""

import logging
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
        self.dashboard_api.register(self.context)
        logger.info(f"[BugCatcher] 插件初始化完成，配置: {self.config}")

    async def initialize(self):
        """插件激活后调用，完成异步初始化。"""
        await self.bug_store.load()
        self.buffer_mgr.start_cleanup_task()
        logger.info("[BugCatcher] 插件已激活，消息缓存系统已启动")

    async def terminate(self):
        """插件禁用时调用，释放资源。"""
        self.buffer_mgr.stop_cleanup_task()
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
        umo = event.unified_msg_origin

        # 白名单 / 全局开关检查
        if not self._should_process(umo):
            return

        # 提取消息信息
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        content = event.get_message_outline()

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
            # Phase 3: 调用分析引擎
            await self._analyze_and_store(umo, trigger.messages)

    # ------------------------------------------------------------------
    # 分析引擎入口（Phase 3 实现）
    # ------------------------------------------------------------------

    async def _analyze_and_store(
        self, umo: str, messages: list
    ) -> None:
        """调用 AI 分析并保存结果。"""
        logger.info(f"[BugCatcher] 开始分析 UMO={umo}，消息数: {len(messages)}")

        try:
            result: AnalysisResult = await self.analyzer.analyze(messages, umo)
        except Exception as e:
            logger.error(f"[BugCatcher] 分析异常: {e}", exc_info=True)
            self.buffer_mgr.clear_buffer(umo)
            return

        if result.error:
            logger.warning(f"[BugCatcher] 分析失败: {result.error}")
            self.buffer_mgr.clear_buffer(umo)
            return

        if result.result == "none":
            logger.info("[BugCatcher] 分析结果: 未发现 bug")
            self.buffer_mgr.clear_buffer(umo)
            return

        log_level = logging.WARNING if result.result == "suspected" else logging.ERROR
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
            records = await self.bug_store.add_bugs_from_analysis(
                umo=umo,
                analysis_result=result,
                raw_messages=messages,
                platform=platform,
            )
            logger.info(f"[BugCatcher] 已保存 {len(records)} 条记录到 BugStore")
        except Exception as e:
            logger.error(f"[BugCatcher] 保存 bug 记录失败: {e}")

        self.buffer_mgr.clear_buffer(umo)
