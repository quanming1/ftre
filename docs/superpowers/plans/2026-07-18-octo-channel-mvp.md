# Octo Channel Plugin MVP 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ftre 上实现一个外部 Octo Channel 插件，单 bot 账号，纯文本收发。

**Architecture:** 单文件 `octo_channel.py`，含三个类：`OctoBotApi`（HTTP 客户端）、`OctoChannel(Channel)`（WebSocket 连接 + 消息路由）、`OctoChannelPlugin(Plugin)`（注册入口）。通过 `Channel.receive()` → Bus → AgentLoop → `Channel.send()` 链路完成入站→处理→出站。依赖 ftre 已实现的 `BEFORE_AGENT_RUN` hook。

**Tech Stack:** Python 3.12, `aiohttp`（WebSocket + HTTP）, ftre Channel 基类 / EventBus / Plugin 体系。

## 全局约束

- 插件放在外部目录 `~/.ftre/plugins/octo_channel.py`
- 配置从 `~/.ftre/config.json` 的 `plugins[].config` 传入
- 单 bot 账号，不做多账户
- 纯文本消息，不处理图片/文件/语音
- 不做重连、heartbeat、typing indicator
- Python 依赖仅 `aiohttp`（需要 `pip install aiohttp`）

---

## 前置任务：提交 BEFORE_AGENT_RUN 改动

### Task 0: 提交 BEFORE_AGENT_RUN 相关改动

**背景：** `BEFORE_AGENT_RUN` hook 是本次 MVP 的前置依赖，当前在 ftre 工作区中已修改但未提交。必须先 commit 这些改动。

**影响的文件（已修改但未提交）：**
- `E:\ftre\src\ftre\plugin\hook_manager.py`
- `E:\ftre\src\ftre\plugin\__init__.py`
- `E:\ftre\src\ftre\agent\loop.py`
- `E:\ftre\src\ftre\plugin\builtin\mcp_plugin.py`
- `E:\ftre\src\ftre\plugin\builtin\skill_plugin.py`
- `E:\ftre\tests\test_plugin_tools.py`
- `E:\ftre\src\ftre\plugin\plugin.py`

- [ ] **Step 1: 验证当前所有测试通过**

```bash
cd E:\ftre && python -m pytest tests/test_plugin_tools.py -v
```

Expected: 11 passed

- [ ] **Step 2: Stage 所有改动并提交**

```bash
cd E:\ftre && git add src/ftre/plugin/hook_manager.py src/ftre/plugin/__init__.py src/ftre/agent/loop.py src/ftre/plugin/builtin/mcp_plugin.py src/ftre/plugin/builtin/skill_plugin.py src/ftre/plugin/plugin.py tests/test_plugin_tools.py
git commit -m "refactor: replace append_system_prompt with BEFORE_AGENT_RUN hook

- 删除 FtrePluginApi.append_system_prompt() 和相关属性
- 新增 BEFORE_AGENT_RUN 挂点 + AgentRunContext(messages list[dict])
- AgentLoop.run() 在 agent.run() 前触发 hook，传入 OpenAI 格式 messages
- mcp/skill 插件迁移到 BEFORE_AGENT_RUN，操作 ctx.messages
- 删除 AgentBuildContext（被 AgentRunContext 取代）"
```

- [ ] **Step 3: Push**

```bash
cd E:\ftre && git push origin master
```

- [ ] **Step 4: 验证 Gateway 启动正常**

```bash
cd E:\ftre && python -m ftre.cli.main gateway run --port 8765
# Ctrl+C 停止后确认无错误
```

---

## 核心实现

### Task 1: 创建测试文件和 OctoBotApi HTTP 客户端

**Files:**
- Create: `E:\ftre\tests\test_octo_channel.py`
- Create: `%USERPROFILE%\.ftre\plugins\octo_channel.py`

**Interfaces:**
- Produces: `OctoBotApi(api_url: str, bot_token: str)` — HTTP 客户端
  - `async register_bot() -> dict` — 返回 `{robot_id, im_token, ws_url, owner_uid}`
  - `async send_message(channel_id: str, channel_type: str, content: str, im_token: str) -> dict`
  - `_session: aiohttp.ClientSession`

- [ ] **Step 1: 写失败的测试**

```python
# E:\ftre\tests\test_octo_channel.py
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aiohttp import web

# 我们会在 Task 2 中创建 OctoBotApi
# 这里先写测试框架，测试会因 import 失败而 FAIL


class TestOctoBotApi:
    """Octo Bot API 客户端测试"""

    async def test_register_bot_returns_credentials(self):
        from octo_channel import OctoBotApi
        api = OctoBotApi("https://api.example.com", "bf_test_token")
        # 构造 mock session
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "robot_id": "test_bot",
            "im_token": "im_test_token",
            "ws_url": "wss://ws.example.com/ws",
            "owner_uid": "uid_123"
        })
        mock_session.post = AsyncMock(return_value=mock_resp)
        api._session = mock_session

        result = await api.register_bot()

        assert result["robot_id"] == "test_bot"
        assert result["im_token"] == "im_test_token"
        assert result["ws_url"] == "wss://ws.example.com/ws"
        mock_session.post.assert_called_once()

    async def test_send_message_calls_api(self):
        from octo_channel import OctoBotApi
        api = OctoBotApi("https://api.example.com", "bf_test_token")
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"message_id": "msg_001"})
        mock_session.post = AsyncMock(return_value=mock_resp)
        api._session = mock_session

        result = await api.send_message(
            channel_id="ch_456",
            channel_type="group",
            content="Hello from ftre",
            im_token="im_test_token"
        )

        assert result["message_id"] == "msg_001"
        call_args = mock_session.post.call_args
        # 验证调用了正确的 URL
        assert "/v1/bot/send_message" in call_args[0][0]


class TestOctoChannel:
    """Octo WebSocket Channel 测试（Task 3 实现）"""

    async def test_start_registers_bot_and_connects_ws(self):
        """start() 应调 register_bot 然后连 WS"""
        pass  # Task 3 实现

    async def test_ws_message_dispatches_to_receive(self):
        """收到 WS 文本帧应转调 receive()"""
        pass  # Task 3 实现

    async def test_send_extracts_text_and_calls_api(self):
        """send() 应从 BusMessage 提取文本并调 send_message"""
        pass  # Task 3 实现
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd E:\ftre && python -m pytest tests/test_octo_channel.py -v 2>&1
```

Expected: 前两个 test FAIL（ImportError: cannot import name 'OctoBotApi'），后三个 SKIP（pass body）

- [ ] **Step 3: 创建 OctoBotApi 实现**

```python
# %USERPROFILE%\.ftre\plugins\octo_channel.py（开头部分）
"""
Octo Channel Plugin for ftre

将 ftre agent 接入 Octo IM 平台，作为群聊 / 私聊 bot。
基于 ftre 的 Channel + EventBus 架构，参考 OpenClaw 的 openclaw-channel-octo。
"""

import asyncio
import json
import logging
from typing import Any

import aiohttp

from ftre.plugin import Plugin, BEFORE_AGENT_RUN
from ftre.channel.base import Channel
from ftre.bus import BusMessage

logger = logging.getLogger("ftre.plugin.octo_channel")

# ——————————————————————————————— Octo Bot API ———————————————————————————————


class OctoBotApi:
    """Octo Bot API HTTP 客户端。

    封装 Octo 平台的 REST API 调用：
    - POST /v1/bot/register    注册 bot，获取 im_token + ws_url
    - POST /v1/bot/send_message 发送消息
    """

    def __init__(self, api_url: str, bot_token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.bot_token = bot_token
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    async def register_bot(self, agent_platform: str = "ftre") -> dict[str, Any]:
        """注册 bot，获取连接凭证。

        Returns:
            {"robot_id": str, "im_token": str, "ws_url": str, "owner_uid": str}
        """
        session = await self._ensure_session()
        async with session.post(
            f"{self.api_url}/v1/bot/register",
            json={"agent_platform": agent_platform},
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Bot 注册失败 ({resp.status}): {data}")
            logger.info(f"[octo] bot 注册成功: robot_id={data.get('robot_id')}")
            return data

    async def send_message(
        self,
        channel_id: str,
        channel_type: str,
        content: str,
        im_token: str,
    ) -> dict[str, Any]:
        """发送文本消息。

        Args:
            channel_id: 群聊 channel_id 或私聊对方 uid
            channel_type: "group" | "direct"
            content: 消息文本
            im_token: bot 的 im_token

        Returns:
            API 响应，含 message_id
        """
        session = await self._ensure_session()
        payload = {
            "channel_id": channel_id,
            "channel_type": channel_type,
            "content": content,
        }
        async with session.post(
            f"{self.api_url}/v1/bot/send_message",
            headers={"Authorization": f"Bearer {im_token}"},
            json=payload,
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"消息发送失败 ({resp.status}): {data}")
            return data

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
```

- [ ] **Step 4: 运行测试验证通过**

```bash
# 先确保 octo_channel.py 在 PYTHONPATH 中
cd E:\ftre && set PYTHONPATH=%USERPROFILE%\.ftre\plugins;%PYTHONPATH% && python -m pytest tests/test_octo_channel.py::TestOctoBotApi -v
```

Expected: 2 passed（test_register_bot_returns_credentials, test_send_message_calls_api）, 3 SKIP

- [ ] **Step 5: Commit**

```bash
cd E:\ftre && git add tests/test_octo_channel.py && git commit -m "test: add OctoBotApi tests"
```

---

### Task 2: 创建 OctoChannel WebSocket 客户端

**Files:**
- Modify: `%USERPROFILE%\.ftre\plugins\octo_channel.py` — 追加 OctoChannel 类
- Modify: `E:\ftre\tests\test_octo_channel.py` — 补全 TestOctoChannel 测试

**Interfaces:**
- Consumes: `OctoBotApi`, `Channel` 基类, `BusMessage`
- Produces: `OctoChannel(Channel)` — WebSocket Channel 实现
  - `async start()` — 注册 bot → 连 WS → 启动消息循环
  - `async _ws_loop()` — 读 WS 帧 → `_handle_message()`
  - `async _handle_message(data: dict)` — 解析消息 → `receive()`
  - `async send(msg: BusMessage)` — 提取文本 → `send_message()`
  - `async stop()` — 断 WS → 关 session

- [ ] **Step 1: 补全测试用例**

```python
# 追加到 E:\ftre\tests\test_octo_channel.py 的 TestOctoChannel 类中

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from ftre.bus import BusMessage

class TestOctoChannel:
    """Octo WebSocket Channel 测试"""

    @pytest.fixture
    def mock_bus(self):
        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        return bus

    @pytest.fixture
    def channel_config(self):
        return {
            "bot_token": "bf_test",
            "api_url": "https://api.example.com",
            "ws_url": "wss://ws.example.com/ws",
        }

    @patch("octo_channel.aiohttp.ClientSession")
    async def test_start_registers_and_connects(self, mock_session_cls, mock_bus, channel_config):
        """start() 应调 register_bot，用返回的 ws_url 连接 WS"""
        from octo_channel import OctoChannel

        # Mock HTTP session for register_bot
        mock_http = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "robot_id": "test_bot",
            "im_token": "im_xxx",
            "ws_url": "wss://ws.example.com/ws",
            "owner_uid": "uid_123",
        })
        mock_http.post = AsyncMock(return_value=mock_resp)

        # Mock WS session
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        mock_session_cls.return_value = mock_session

        ch = OctoChannel(channel_config, mock_bus)
        # 注入 mock http session
        ch.api._session = mock_http

        await ch.start()

        # 验证调了 register_bot
        mock_http.post.assert_called()
        # 验证连了 WS
        mock_session.ws_connect.assert_called_once_with("wss://ws.example.com/ws")

    async def test_send_extracts_text_and_calls_api(self, mock_bus, channel_config):
        """send() 应从 BusMessage 提取 content 文本并调 send_message"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)
        ch.api.send_message = AsyncMock(return_value={"message_id": "msg_1"})
        ch._im_token = "im_xxx"

        msg = BusMessage(
            type="agent_event",
            from_channel="octo",
            from_session="octo_uid_alice_ch_group_1",
            to_channel="octo",
            to_session="octo_uid_alice_ch_group_1",
            data={
                "type": "assistant_message_complete",
                "data": {"content": "你好，我是 ftre bot"},
            },
        )

        await ch.send(msg)

        ch.api.send_message.assert_called_once()
        call_kwargs = ch.api.send_message.call_args[1]
        assert "你好，我是 ftre bot" in call_kwargs["content"]

    async def test_ws_text_frame_triggers_receive(self, mock_bus, channel_config):
        """收到 WS TEXT 帧应解析为 user_message 并调 receive()"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)
        ch._im_token = "im_xxx"

        data = {
            "type": 1,  # text message
            "content": "你好 bot",
            "from_uid": "uid_alice",
            "channel_id": "ch_group_1",
            "channel_type": 1,  # group
            "message_id": "msg_in_1",
            "timestamp": 1719700000,
        }

        await ch._handle_message(data)

        # 验证调了 receive()
        mock_bus.publish_inbound.assert_called_once()
        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.type == "user_message"
        assert call_msg.data["content"] == "你好 bot"
        assert call_msg.data["from_uid"] == "uid_alice"

    async def test_stop_closes_ws_and_session(self, mock_bus, channel_config):
        """stop() 应关闭 WS 连接和 HTTP session"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)
        mock_ws = AsyncMock()
        mock_ws.closed = False
        ch._ws = mock_ws
        mock_session = MagicMock()
        mock_session.close = AsyncMock()
        ch._session = mock_session

        await ch.stop()

        mock_ws.close.assert_called_once()
        mock_session.close.assert_called_once()
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd E:\ftre && set PYTHONPATH=%USERPROFILE%\.ftre\plugins;%PYTHONPATH% && python -m pytest tests/test_octo_channel.py::TestOctoChannel -v
```

Expected: 4 FAIL（import 或属性缺失）

- [ ] **Step 3: 实现 OctoChannel**

```python
# 追加到 %USERPROFILE%\.ftre\plugins\octo_channel.py


# ——————————————————————————————— Octo WebSocket Channel ——————————————————————


class OctoChannel(Channel):
    """Octo WebSocket Channel。

    收：WebSocket 文本帧 → 解析 content/from_uid/channel_id → BusMessage
    发：BusMessage agent_event → 提取文本 → Octo sendMessage API
    """

    def __init__(
        self,
        config: dict,
        bus: "EventBus",
        channel_id: str = "octo",
        name: str = "Octo Channel",
    ) -> None:
        super().__init__(channel_id, name, bus)
        self.config = config
        self.api = OctoBotApi(config["api_url"], config["bot_token"])
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._im_token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None

    async def start(self) -> None:
        """注册 bot → 连 WebSocket → 启动消息循环。"""
        # 1. 注册 bot 拿凭证
        credentials = await self.api.register_bot()
        self._im_token = credentials["im_token"]
        ws_url = credentials["ws_url"]
        owner_uid = credentials.get("owner_uid", "")
        logger.info(
            f"[octo] bot 注册成功: robot_id={credentials['robot_id']}, "
            f"owner_uid={owner_uid}"
        )

        # 2. 连 WebSocket
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(ws_url)
        logger.info(f"[octo] WS 已连接: {ws_url}")

        # 3. 启动消息循环（后台 task）
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self) -> None:
        """WebSocket 消息循环。"""
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        logger.warning(f"[octo] 无法解析 WS 消息: {msg.data[:200]}")
                    except Exception:
                        logger.exception("[octo] 处理 WS 消息异常")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"[octo] WS 错误: {self._ws.exception()}")
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.info("[octo] WS 连接关闭")
                    break
        except asyncio.CancelledError:
            logger.info("[octo] WS 消息循环被取消")
        except Exception:
            logger.exception("[octo] WS 消息循环异常")

    async def _handle_message(self, data: dict) -> None:
        """解析单条 WS 消息 → 投递到 Bus。

        Octo WS 消息格式：
        {
            "type": 1,              // 1=text, 2=image, ...
            "content": "消息文本",
            "from_uid": "uid_xxx",
            "channel_id": "ch_xxx",  // 群聊 channel_id 或私聊格式 "s{space}_{peer}"
            "channel_type": 1,       // 1=group, 2=direct
            "message_id": "msg_xxx",
            "timestamp": 1719700000,
        }
        """
        msg_type = data.get("type")
        if msg_type != 1:  # 非文本消息，MVP 跳过
            logger.debug(f"[octo] 跳过非文本消息 type={msg_type}")
            return

        content = data.get("content", "")
        from_uid = data.get("from_uid", "")
        channel_id = data.get("channel_id", "")
        channel_type = data.get("channel_type", 1)
        message_id = data.get("message_id", "")

        # 构造 session_id：每个 Octo 对话对应一个 ftre session
        session_id = f"octo_{from_uid}_{channel_id}"

        await self.receive(
            session_id=session_id,
            data={
                "content": content,
                "from_uid": from_uid,
                "channel_id": channel_id,
                "channel_type": channel_type,
                "message_id": message_id,
            },
            metadata={"octo_message_id": message_id},
        )
        logger.debug(f"[octo] 入站: session={session_id} content={content[:50]}...")

    async def send(self, msg: BusMessage) -> None:
        """推送 outbound 消息到 Octo。

        BusMessage.data 是 agent_event，格式：
        {"type": "assistant_message_complete", "data": {"content": "回复文本"}}
        """
        event_type = msg.data.get("type", "")
        event_data = msg.data.get("data", {})

        # 只发送完整的 assistant 回复（流式增量 assistant_message 忽略）
        if event_type not in ("assistant_message_complete",):
            return

        content = event_data.get("content", "")
        if not content:
            return

        # 从 session_id 解析会话信息
        session_id = msg.to_session or msg.from_session
        # session_id 格式: "octo_{from_uid}_{channel_id}"
        parts = session_id.split("_", 2)  # ["octo", "from_uid", "channel_id"]
        if len(parts) >= 3:
            channel_id = parts[2]
        else:
            logger.warning(f"[octo] 无法从 session_id 解析 channel_id: {session_id}")
            return

        channel_type = msg.data.get("data", {}).get("channel_type", "group")

        if self._im_token is None:
            logger.error("[octo] im_token 未设置，无法发送消息")
            return

        try:
            result = await self.api.send_message(
                channel_id=channel_id,
                channel_type=channel_type,
                content=content,
                im_token=self._im_token,
            )
            logger.debug(f"[octo] 出站: message_id={result.get('message_id')} content={content[:50]}...")
        except Exception:
            logger.exception("[octo] 发送消息失败")

    async def stop(self) -> None:
        """断开 WS 连接，取消消息循环，关闭 HTTP session。"""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws and not self._ws.closed:
            await self._ws.close()
            logger.info("[octo] WS 连接已关闭")

        if self._session:
            await self._session.close()

        await self.api.close()
        logger.info("[octo] Channel 已停止")
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd E:\ftre && set PYTHONPATH=%USERPROFILE%\.ftre\plugins;%PYTHONPATH% && python -m pytest tests/test_octo_channel.py -v
```

Expected: 6 passed（TestOctoBotApi 2 + TestOctoChannel 4）

- [ ] **Step 5: Commit**

```bash
cd E:\ftre && git add tests/test_octo_channel.py && git commit -m "test: add OctoChannel WebSocket tests"
```

---

### Task 3: 创建 Plugin 入口 + before_agent_run hook

**Files:**
- Modify: `%USERPROFILE%\.ftre\plugins\octo_channel.py` — 追加 Plugin 类

- [ ] **Step 1: 实现 OctoChannelPlugin**

```python
# 追加到 octo_channel.py 末尾


# ——————————————————————————————— Plugin 入口 ——————————————————————————————————


class OctoChannelPlugin(Plugin):
    """Octo Channel Plugin。

    注册 Octo WebSocket Channel + before_agent_run hook。
    """

    name = "octo_channel"
    version = "1.0.0"

    def setup(self) -> None:
        config = self.api.config or {}

        # 注册 Channel（单 bot）
        channel = OctoChannel(config, self.api.bus)
        self.api.register_channel(channel)
        logger.info(f"[octo] Channel 已注册")

        # 注册 BEFORE_AGENT_RUN hook —— 注入 Octo 上下文到 messages
        self.api.register_hook(BEFORE_AGENT_RUN, self._on_agent_run)

    def _on_agent_run(self, ctx):
        """BEFORE_AGENT_RUN hook: 在 agent.run() 前注入 Octo 相关上下文。

        参考 OpenClaw 的 prependSystemContext：
        - 如果 messages 中有 system 消息，追加 Octo 相关提示
        - 如果没有，插入一条新的 system 消息
        """
        hint = (
            "你是 Octo IM 平台上的一个 bot。"
            "你可以通过 `send_message` 向用户发送消息。"
        )
        for msg in ctx.messages:
            if msg.get("role") == "system":
                if hint not in msg["content"]:
                    msg["content"] = f"{msg['content']}\n\n{hint}"
                break
        else:
            ctx.messages.insert(0, {"role": "system", "content": hint})
        return ctx

    def teardown(self) -> None:
        pass
```

- [ ] **Step 2: 配置 config.json**

```json
// %USERPROFILE%\.ftre\config.json 中 plugins 数组追加：
{
  "plugins": [
    {
      "name": "octo_channel",
      "enabled": true,
      "config": {
        "bot_token": "bf_your_bot_token_here",
        "api_url": "https://your-octo-server.example.com",
        "ws_url": "wss://ws.your-octo-server.example.com"
      }
    }
  ]
}
```

- [ ] **Step 3: 增加 before_agent_run hook 测试**

```python
# 追加到 E:\ftre\tests\test_octo_channel.py

class TestOctoChannelPlugin:

    def test_hook_injects_octo_hint_into_system_message(self):
        """BEFORE_AGENT_RUN hook 应在 system 消息中注入 Octo 提示"""
        from ftre.plugin import AgentRunContext, BEFORE_AGENT_RUN, HookManager
        # 直接测试 hook 函数，不加载完整 Plugin
        hooks = HookManager()
        from octo_channel import OctoChannelPlugin
        plugin = OctoChannelPlugin()
        plugin._on_agent_run  # 确保方法存在

        # 模拟 plugin 的 hook 注册
        hooks.register(BEFORE_AGENT_RUN, plugin._on_agent_run)

        ctx = AgentRunContext(
            session_id="sess_1",
            channel_id="octo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
            config=None,
        )
        result = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

        system_msg = result.messages[0]
        assert system_msg["role"] == "system"
        assert "Octo IM 平台" in system_msg["content"]

    def test_hook_inserts_system_message_when_none_exists(self):
        """如果没有 system 消息，应插入一条新的"""
        from ftre.plugin import AgentRunContext, BEFORE_AGENT_RUN, HookManager
        hooks = HookManager()
        from octo_channel import OctoChannelPlugin
        plugin = OctoChannelPlugin()
        hooks.register(BEFORE_AGENT_RUN, plugin._on_agent_run)

        ctx = AgentRunContext(
            session_id="sess_1",
            channel_id="octo",
            messages=[
                {"role": "user", "content": "Hello"},
            ],
            config=None,
        )
        result = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

        assert result.messages[0]["role"] == "system"
        assert "Octo IM 平台" in result.messages[0]["content"]
```

- [ ] **Step 4: 运行全部测试**

```bash
cd E:\ftre && set PYTHONPATH=%USERPROFILE%\.ftre\plugins;%PYTHONPATH% && python -m pytest tests/test_octo_channel.py -v
```

Expected: 8 passed（TestOctoBotApi 2 + TestOctoChannel 4 + TestOctoChannelPlugin 2）

- [ ] **Step 5: Commit**

```bash
cd E:\ftre && git add tests/test_octo_channel.py && git commit -m "test: add OctoChannelPlugin hook tests"
```

---

### Task 4: 端到端集成测试

**Files:**
- Modify: `E:\ftre\tests\test_octo_channel.py` — 追加集成测试

- [ ] **Step 1: 写端到端测试（mock Octo API + WS）**

```python
# 追加到 E:\ftre\tests\test_octo_channel.py

class TestOctoChannelIntegration:
    """端到端：从 WS 入站到 Agent 回复出站的完整链路"""

    @pytest.mark.asyncio
    async def test_full_round_trip_ws_to_send(self):
        """模拟：WS 收到消息 → AgentLoop 处理 → Channel.send() 回复"""
        from octo_channel import OctoChannel, OctoBotApi
        from unittest.mock import AsyncMock, MagicMock

        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        bus.publish_outbound = AsyncMock()

        config = {
            "bot_token": "bf_test",
            "api_url": "https://api.example.com",
            "ws_url": "wss://ws.example.com/ws",
        }
        ch = OctoChannel(config, bus)
        ch._im_token = "im_test_token"
        ch.api.send_message = AsyncMock(return_value={"message_id": "reply_1"})

        # Step 1: 模拟 WS 收到消息
        ws_data = {
            "type": 1,
            "content": "你好，帮我查一下天气",
            "from_uid": "uid_alice",
            "channel_id": "ch_group_1",
            "channel_type": 1,
            "message_id": "in_1",
            "timestamp": 1719700000,
        }
        await ch._handle_message(ws_data)

        # 验证 publish_inbound 被调用
        bus.publish_inbound.assert_called_once()
        inbound_msg = bus.publish_inbound.call_args[0][0]
        assert inbound_msg.type == "user_message"
        assert inbound_msg.data["content"] == "你好，帮我查一下天气"
        assert "octo_" in inbound_msg.from_session

        # Step 2: 模拟 AgentLoop 处理后推送 outbound 到 Channel.send()
        outbound_msg = BusMessage(
            type="agent_event",
            from_channel="octo",
            from_session=inbound_msg.from_session,
            to_channel="octo",
            to_session=inbound_msg.from_session,
            data={
                "type": "assistant_message_complete",
                "data": {"content": "今天上海晴，25°C"},
            },
        )
        await ch.send(outbound_msg)

        # 验证 send_message 被调用
        ch.api.send_message.assert_called_once()
        call_kwargs = ch.api.send_message.call_args[1]
        assert "今天上海晴" in call_kwargs["content"]
        assert call_kwargs["im_token"] == "im_test_token"
```

- [ ] **Step 2: 运行集成测试**

```bash
cd E:\ftre && set PYTHONPATH=%USERPROFILE%\.ftre\plugins;%PYTHONPATH% && python -m pytest tests/test_octo_channel.py::TestOctoChannelIntegration -v
```

Expected: 1 passed

- [ ] **Step 3: Commit**

```bash
cd E:\ftre && git add tests/test_octo_channel.py && git commit -m "test: add Octo channel integration test"
```

---

## 自检

**Spec coverage:**
- [x] OctoBotApi HTTP 客户端 — Task 1
- [x] OctoChannel WebSocket 连接 — Task 2
- [x] WS 消息解析 + receive() — Task 2
- [x] send() 提取文本 + 调 API — Task 2
- [x] Plugin 入口 + 注册 Channel — Task 3
- [x] BEFORE_AGENT_RUN hook 注入 — Task 3
- [x] 端到端集成测试 — Task 4
- [x] 前置依赖提交 — Task 0

**Type consistency:**
- `OctoBotApi(api_url, bot_token)` — Task 1 定义，Task 2 使用 ✓
- `OctoChannel(config, bus, channel_id, name)` — Task 2 定义，Task 3/4 使用 ✓
- `Channel.receive(session_id, data, metadata)` — Channel 基类定义 ✓
- `Channel.send(msg: BusMessage)` — Channel 基类定义 ✓

**No placeholders:** 所有步骤都有完整代码，无 TBD/TODO。
