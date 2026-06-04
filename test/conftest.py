"""
Pytest 配置和全局 fixtures。

在测试收集阶段预先 mock 所有 AstrBot 依赖，
使插件模块可以在没有 AstrBot 环境的情况下被导入和测试。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# 将插件目录添加到 Python 路径（供绝对导入使用）
_PLUGIN_DIR = Path(__file__).parent.parent.resolve()
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

# =====================================================================
# 1. 预注入 AstrBot 依赖 mock（必须在任何插件导入之前完成）
# =====================================================================


def _make_magic_module(name: str) -> MagicMock:
    """创建一个可嵌套属性的 MagicMock 模块。"""
    mod = MagicMock()
    mod.__name__ = name
    return mod


# astrbot 根
astrbot_pkg = _make_magic_module("astrbot")
sys.modules["astrbot"] = astrbot_pkg

# astrbot.api
api_mod = _make_magic_module("astrbot.api")
# logger: 模拟 logging.Logger 接口
_logger = MagicMock()
_logger.info = MagicMock()
_logger.warning = MagicMock()
_logger.error = MagicMock()
_logger.debug = MagicMock()
_logger.log = MagicMock()
api_mod.logger = _logger
sys.modules["astrbot.api"] = api_mod

# astrbot.api.star
star_mod = _make_magic_module("astrbot.api.star")


class _MockContext:
    """模拟 AstrBot Context 的核心方法。"""

    def __init__(self):
        self.llm_generate = AsyncMock()
        self.get_using_provider = MagicMock(return_value=None)
        self.get_all_providers = MagicMock(return_value=[])
        self.register_web_api = MagicMock()


class _MockStar:
    """模拟 Star 基类。"""

    def __init__(self, context=None, config=None):
        self.context = context

    async def initialize(self):
        pass

    async def terminate(self):
        pass


star_mod.Context = _MockContext
star_mod.Star = _MockStar
sys.modules["astrbot.api.star"] = star_mod

# astrbot.api.event
event_mod = _make_magic_module("astrbot.api.event")


class _MockAstrMessageEvent:
    """模拟 AstrMessageEvent。"""

    def __init__(self, **kwargs):
        self._umo = kwargs.get("umo", "aiocqhttp:GROUP_MESSAGE:123456")
        self._sender_id = kwargs.get("sender_id", "user_001")
        self._sender_name = kwargs.get("sender_name", "测试用户")
        self._content = kwargs.get("content", "测试消息")
        self._outline = kwargs.get("outline", self._content)

    @property
    def unified_msg_origin(self) -> str:
        return self._umo

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_message_outline(self) -> str:
        return self._outline

    def get_group_id(self) -> str:
        parts = self._umo.split(":")
        return parts[2] if len(parts) >= 3 else ""

    def get_platform_name(self) -> str:
        parts = self._umo.split(":")
        return parts[0] if parts else ""

    def get_message_type(self):
        return "GROUP_MESSAGE"


event_mod.AstrMessageEvent = _MockAstrMessageEvent
sys.modules["astrbot.api.event"] = event_mod

# astrbot.api.event.filter
filter_mod = _make_magic_module("astrbot.api.event.filter")


class _MockEventMessageType:
    GROUP_MESSAGE = "GroupMessage"
    PRIVATE_MESSAGE = "PrivateMessage"
    OTHER_MESSAGE = "OtherMessage"
    ALL = "All"


def _event_message_type_filter(event_type, priority=0):
    """模拟装饰器。"""

    def decorator(func):
        func._event_filter = event_type
        func._priority = priority
        return func

    return decorator


filter_mod.event_message_type = _event_message_type_filter
filter_mod.EventMessageType = _MockEventMessageType
sys.modules["astrbot.api.event.filter"] = filter_mod

# astrbot.api.provider
provider_mod = _make_magic_module("astrbot.api.provider")


class _MockProviderMeta:
    def __init__(self, id: str = "test_provider"):
        self.id = id


class _MockProvider:
    def __init__(self, id: str = "test_provider"):
        self._meta = _MockProviderMeta(id)

    def meta(self):
        return self._meta


provider_mod.Provider = _MockProvider
sys.modules["astrbot.api.provider"] = provider_mod

# astrbot.core
sys.modules["astrbot.core"] = _make_magic_module("astrbot.core")

# astrbot.core.utils
sys.modules["astrbot.core.utils"] = _make_magic_module("astrbot.core.utils")

# astrbot.core.utils.astrbot_path
_path_mod = _make_magic_module("astrbot.core.utils.astrbot_path")
_path_mod.get_astrbot_data_path = MagicMock(
    return_value=str(Path(tempfile.gettempdir()) / "astrbot_test_data")
)
sys.modules["astrbot.core.utils.astrbot_path"] = _path_mod

# quart（Dashboard API 依赖）
_quart_mod = _make_magic_module("quart")
_quart_mod.jsonify = lambda *args, **kwargs: MagicMock(
    status_code=200,
    get_json=AsyncMock(return_value=args[0] if args else kwargs),
)
_mock_request = MagicMock()
_mock_request.args = {}
_mock_request.method = "GET"
_mock_request.get_json = AsyncMock(return_value={})
_quart_mod.request = _mock_request
sys.modules["quart"] = _quart_mod

# =====================================================================
# 2. Fixtures
# =====================================================================


@pytest.fixture
def temp_data_dir(monkeypatch):
    """为每次测试提供独立的临时数据目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(
            "astrbot.core.utils.astrbot_path.get_astrbot_data_path",
            lambda: tmpdir,
        )
        yield Path(tmpdir)


@pytest.fixture
def mock_context():
    """提供预配置的 Mock Context。"""
    ctx = _MockContext()
    return ctx


@pytest.fixture
def default_config():
    """默认插件配置。"""
    return {
        "global_mode": False,
        "umo_whitelist": [],
        "batch_size": 10,
        "max_history": 20,
        "time_threshold_min": 5,
        "analysis_interval_min": 1,
        "provider_id": "",
        "diagnostics_max_entries": 200,
    }


@pytest.fixture
def sample_messages():
    """提供一组测试用的 MessageRecord 列表。"""
    from astrbot_plugin_bug_catcher.chat_buffer import MessageRecord

    return [
        MessageRecord(
            timestamp=1717450000.0 + i * 60,
            sender_id=f"user_{i:03d}",
            sender_name=f"用户{i}",
            content=f"这是第 {i} 条测试消息",
        )
        for i in range(15)
    ]


@pytest.fixture
def bug_analysis_confirmed():
    """提供 confirmed 分析结果。"""
    from astrbot_plugin_bug_catcher.analyzer import AnalysisResult, BugItem

    return AnalysisResult(
        result="confirmed",
        bugs=[
            BugItem(
                severity="high",
                summary="测试 bug 摘要",
                analysis="这是一个测试用的 AI 分析结果",
                related_messages=[0, 1, 2],
            )
        ],
    )


@pytest.fixture
def bug_analysis_none():
    """提供 none 分析结果。"""
    from astrbot_plugin_bug_catcher.analyzer import AnalysisResult

    return AnalysisResult(result="none")


@pytest.fixture
def mock_llm_response():
    """模拟 LLM 正常 JSON 响应。"""
    resp = MagicMock()
    resp.completion_text = (
        '{"result": "confirmed", "bugs": [{"severity": "high", '
        '"summary": "模块加载失败", "analysis": "用户报告加载插件时报错", '
        '"related_messages": [0, 1]}]}'
    )
    return resp


@pytest.fixture
def mock_llm_bad_json():
    """模拟 LLM 非 JSON 响应。"""
    resp = MagicMock()
    resp.completion_text = "抱歉，我无法分析这段聊天记录。"
    return resp


@pytest.fixture
def mock_llm_json_with_markdown():
    """模拟 LLM 返回 markdown 包裹的 JSON。"""
    resp = MagicMock()
    resp.completion_text = (
        '```json\n{"result": "suspected", "bugs": [{"severity": "medium", '
        '"summary": "可能的兼容性问题", "analysis": "需要进一步确认", '
        '"related_messages": [2]}]}\n```'
    )
    return resp
