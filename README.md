# AstrBot Bug Catcher Plugin

自动监听 AstrBot 开发者群聊消息，利用 AI 智能识别 bug 反馈、错误报告、功能缺陷描述，并将确认的 bug 记录到 Dashboard 面板供开发者查看和管理。

## Features 功能特性

- **Silent Monitor 静默监听**：后台监听群聊消息，不回复不干扰正常对话
- **AI Intelligent Analysis AI 智能分析**：调用 LLM 分析聊天记录，自动识别 bug 反馈并分级（`confirmed` / `suspected` / `none`）
- **Multi-bug Detection 多 Bug 识别**：一次分析可识别多条独立的 bug 报告，不会将不同问题合并（v1.1.1）
- **Bug Deduplication Bug 去重**：AI 分析时参考已记录的 open bug 列表，自动判断是否重复报告并合并记录（v1.1.0）
- **Report History 汇报历史**：记录每次 bug 被报告的时间、群聊和报告者，Dashboard 可追溯完整汇报链（v1.1.0）
- **Media Sanitization 媒体清洗**：自动清洗消息中的图片/视频 URL，替换为占位符，避免多模态模型报错（v1.1.0）
- **Whitelist / Global Mode 白名单与全局模式**：支持按 UMO 白名单精准控制监听范围，或开启全局模式监听所有群聊
- **Dual Threshold Trigger 双阈值触发**：消息数量达到 `batch_size` 或距离上次分析超过 `time_threshold_min` 分钟时自动触发分析
- **Multi-layer JSON Tolerance 多层 JSON 容错**：LLM 返回非标准 JSON 时自动尝试正则提取和格式修复，极大降低解析失败率
- **Dashboard Panel Dashboard 面板**：WebUI 可视化展示 bug 列表，支持按严重程度 / 状态 / 结果筛选、分页浏览、详情弹窗、原始消息追溯
- **Atomic Persistence 原子持久化**：临时文件 + `os.replace()` 原子重命名，防止写入中断导致 JSON 损坏
- **Concurrency Safe 并发安全**：每 UMO 独立 `asyncio.Lock`，分析任务互斥，TTL 清理加锁防竞态（v1.1.1）
- **Multi-platform 多平台支持**：适配所有 AstrBot 支持的消息平台

## Installation 安装

```bash
# 1. 将插件目录复制到 AstrBot 的 data/plugins/ 目录
cp -r astrbot_plugin_bug_catcher /path/to/astrbot/data/plugins/

# 2. 在 AstrBot WebUI -> 插件管理 -> 重载插件
# 3. 在 WebUI -> 插件配置 -> Bug Catcher 中设置参数
# 4. 激活插件
```

## Configuration 配置项

| 配置项 | 类型 | 说明 | 默认值 |
|--------|------|------|--------|
| `global_mode` | bool | 全局模式：开启后监听所有群聊，忽略白名单 | `false` |
| `umo_whitelist` | list | 白名单 UMO 列表（格式：`platform:type:session_id`） | `[]` |
| `batch_size` | int | 每轮收集的消息数，达到此数量触发 AI 分析 | `200` |
| `max_history` | int | 单个群聊的最大消息缓存数（FIFO 淘汰） | `300` |
| `time_threshold_min` | int | 时间阈值（分钟）：低活跃群聊超过此时间强制触发分析 | `30` |
| `analysis_interval_min` | int | 同一群聊的分析冷却时间（分钟），防止频繁调用 LLM（最小值 1） | `5` |
| `provider_id` | string | 用于 Bug 分析的 LLM 提供商（留空使用默认模型） | 空（默认模型） |

## Usage 使用方式

1. 在 AstrBot WebUI -> 插件管理 -> 激活 Bug Catcher 插件
2. 在配置面板中设置监听范围（白名单或全局）和分析参数
3. 插件开始静默监听群聊消息，达到阈值后自动调用 LLM 分析
4. 访问 Dashboard -> 插件页面 -> Bug Catcher 查看识别的 bug 记录
5. 在 Dashboard 中查看详情、标记已解决 / 忽略、删除记录

## Architecture 架构

```
消息总线 -> @event_message_type(GROUP_MESSAGE) -> 白名单/全局开关
         -> ChatBufferManager (deque + 双阈值触发 + TTL清理)
         -> BugAnalyzer (System Prompt + LLM + 三层 JSON 容错 + 图片清洗 + 去重)
         -> BugStore (JSON 原子写入 + report_history + 统计同步)
         -> Dashboard API (4 routes) -> Dashboard Page (纯 HTML/CSS/JS, no emoji)
```

## Project Structure 文件结构

```
astrbot_plugin_bug_catcher/
|-- main.py              # 插件主类（Star 子类，消息监听 + 分析触发 + 模块协调）
|-- chat_buffer.py       # 消息缓存管理器（FIFO deque、batch_size/时间双阈值、TTL 清理）
|-- analyzer.py          # AI 分析引擎（Prompt 构建、Token 截断、LLM 调用、三层 JSON 容错、图片清洗）
|-- bug_store.py         # Bug 持久化存储（CRUD、分页查询、report_history、原子 rename 写入）
|-- dashboard_api.py     # Dashboard 后端 API（4 个 GET/POST 路由封装）
|-- metadata.yaml        # 插件元数据（v1.1.1）
|-- _conf_schema.json    # 配置项定义（7 项，含 _special: "select_provider"）
|-- pytest.ini           # Pytest 配置
|-- requirements-test.txt # 测试依赖
|-- .gitignore
|-- .github/
|   `-- workflows/
|       `-- ci.yml       # GitHub Actions CI（Python 3.12/3.13 矩阵 + Ruff lint）
|-- test/
|   |-- conftest.py      # 全局 fixtures + AstrBot/Quart 依赖 mock（零 AstrBot 运行）
|   |-- test_chat_buffer.py
|   |-- test_analyzer.py
|   |-- test_bug_store.py
|   |-- test_dashboard_api.py
|   `-- test_integration.py
`-- pages/
    `-- dashboard/
        |-- index.html   # 页面骨架
        |-- style.css    # 深色主题响应式样式
        `-- app.js       # Bridge SDK 通信 + 业务逻辑
```

## Testing 测试

```bash
# 安装测试依赖
pip install -r requirements-test.txt

# 运行全部测试（70 项）
pytest test/ -v

# 运行指定模块
pytest test/test_analyzer.py -v

# 带覆盖率报告
pytest test/ --cov=astrbot_plugin_bug_catcher --cov-report=term-missing
```

测试不依赖 AstrBot 环境，`conftest.py` 预注入了完整的 AstrBot API mock。

## Requirements 环境要求

- Python >= 3.12
- AstrBot v4.x

## License 许可证

MIT License (c) 2026 C2H2SNO6

### 使用声明

本项目基于 MIT 许可证开放。你可以自由使用、修改、分发，包括商业用途。

但有一件事我恳请每一位使用者：**请不要在仅做最低限度修改（如改个名字、换个图标、删除版权声明）后，将其作为独立产品直接售卖。** 这种行为并非"二次开发"，而是对原作者劳动成果的轻慢，也对购买者构成了误导。

如果你基于本项目进行了**实质性改进**——无论是功能增强、性能优化、适配新的场景，还是将其融入更大的解决方案——我欢迎你以此开展商业活动，并真诚地祝你成功。我只希望你在适当位置保留一行上游来源的说明，让后来者知道这座房子的地基是谁打的。

**关于维权：** 我没有精力、也不打算对任何使用者采取法律行动。写下这段话，只是因为我觉得"改个名字就卖"这件事很过分，而我想让你知道这一点。如果你是一个有尊严的开发者，相信你已经明白了我的意思。

> This project is released under the MIT License. Please do not repackage it as a standalone product after only cosmetic changes. If you have made substantial improvements, I welcome you to build a business around it. I only ask for a line of attribution. I will not take legal action against anyone; I write this simply because reselling with a new name is deeply disrespectful.
