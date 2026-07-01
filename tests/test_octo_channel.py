import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from ftre.bus import BusMessage


class TestOctoBotApi:
    """Octo Bot API 客户端测试"""

    @pytest.mark.asyncio
    async def test_register_bot_returns_credentials(self):
        from octo_channel import OctoBotApi
        api = OctoBotApi("https://api.example.com", "bf_test_token")
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "robot_id": "test_bot",
            "im_token": "im_test_token",
            "ws_url": "wss://ws.example.com/ws",
            "owner_uid": "uid_123"
        })
        mock_resp.__aenter__.return_value = mock_resp
        mock_session.post = MagicMock(return_value=mock_resp)
        api._session = mock_session

        result = await api.register_bot()

        assert result["robot_id"] == "test_bot"
        assert result["im_token"] == "im_test_token"
        assert result["ws_url"] == "wss://ws.example.com/ws"
        mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_calls_api(self):
        from octo_channel import OctoBotApi
        api = OctoBotApi("https://api.example.com", "bf_test_token")
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"message_id": "msg_001"})
        mock_resp.__aenter__.return_value = mock_resp
        mock_session.post = MagicMock(return_value=mock_resp)
        api._session = mock_session

        result = await api.send_message(
            channel_id="ch_456",
            channel_type=2,
            content="Hello from ftre",
        )

        assert result["message_id"] == "msg_001"
        call_args = mock_session.post.call_args
        # URL 应是驼峰格式
        assert "/v1/bot/sendMessage" in call_args[0][0]
        # payload 格式：嵌套结构
        payload = call_args[1]["json"]
        assert payload["channel_id"] == "ch_456"
        assert payload["channel_type"] == 2
        assert payload["payload"]["type"] == 1
        assert payload["payload"]["content"] == "Hello from ftre"


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

    @pytest.mark.asyncio
    @patch("octo_channel.aiohttp.ClientSession")
    async def test_start_registers_and_connects(self, mock_session_cls, mock_bus, channel_config):
        """start() should call register_bot, then connect WS with returned ws_url"""
        from octo_channel import OctoChannel

        mock_http = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "robot_id": "test_bot",
            "im_token": "im_xxx",
            "ws_url": "wss://ws.example.com/ws",
            "owner_uid": "uid_123",
        })
        mock_resp.__aenter__.return_value = mock_resp
        mock_http.post = MagicMock(return_value=mock_resp)

        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        mock_session_cls.return_value = mock_session

        ch = OctoChannel(channel_config, mock_bus)
        ch.api._session = mock_http

        await ch.start()

        mock_http.post.assert_called()
        mock_session.ws_connect.assert_called_once_with("wss://ws.example.com/ws")

    @pytest.mark.asyncio
    async def test_send_extracts_text_and_calls_api(self, mock_bus, channel_config):
        """send() should extract text from BusMessage and call send_message with correct channel_type"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)
        ch.api.send_message = AsyncMock(return_value={"message_id": "msg_1"})

        # session_id 格式: octo_{channel_type}_{channel_id}
        msg = BusMessage(
            type="agent_event",
            from_channel="octo",
            from_session="octo_2_ch_group_1",
            to_channel="octo",
            to_session="octo_2_ch_group_1",
            data={
                "type": "assistant_message_complete",
                "data": {"content": "Hello, I am ftre bot"},
            },
        )

        await ch.send(msg)

        ch.api.send_message.assert_called_once()
        call_kwargs = ch.api.send_message.call_args[1]
        assert "Hello, I am ftre bot" in call_kwargs["content"]
        assert call_kwargs["channel_type"] == 2
        assert call_kwargs["channel_id"] == "ch_group_1"

    @pytest.mark.asyncio
    async def test_ws_text_frame_triggers_receive(self, mock_bus, channel_config):
        """WS TEXT frame (nested event format) should parse to user_message and call receive()"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        # Octo WS 事件是嵌套结构
        event = {
            "event_id": 102,
            "message": {
                "message_id": 1002,
                "from_uid": "uid_alice",
                "channel_id": "ch_group_1",
                "channel_type": 2,
                "payload": {"type": 1, "content": "Hello bot"},
                "timestamp": 1719700000,
            },
        }

        await ch._handle_message(event)

        mock_bus.publish_inbound.assert_called_once()
        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.type == "user_message"
        assert call_msg.data["content"] == "Hello bot"
        assert call_msg.data["from_uid"] == "uid_alice"
        assert call_msg.data["channel_type"] == 2
        # session_id 应编码 channel_type
        assert "octo_2_" in call_msg.from_session

    @pytest.mark.asyncio
    async def test_ws_dm_event_uses_from_uid_as_channel(self, mock_bus, channel_config):
        """DM 事件无 channel_id，应使用 from_uid 作为 channel_id，channel_type=1"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        # DM 事件：无 channel_id / channel_type
        event = {
            "event_id": 101,
            "message": {
                "message_id": 1001,
                "from_uid": "uid_alice",
                "payload": {"type": 1, "content": "Hi bot!"},
                "timestamp": 1700000000,
            },
        }

        await ch._handle_message(event)

        mock_bus.publish_inbound.assert_called_once()
        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.data["channel_type"] == 1  # DM
        assert call_msg.data["channel_id"] == "uid_alice"
        assert call_msg.from_session == "octo_1_uid_alice"

    @pytest.mark.asyncio
    async def test_stop_closes_ws_and_session(self, mock_bus, channel_config):
        """stop() should close WS connection and HTTP session"""
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


class TestOctoChannelPlugin:
    """Plugin hook 测试"""

    def test_hook_injects_octo_hint_into_system_message(self):
        """BEFORE_AGENT_RUN hook 应在 system 消息中注入 Octo 提示"""
        from ftre.plugin import AgentRunContext, BEFORE_AGENT_RUN, HookManager
        hooks = HookManager()
        from octo_channel import OctoChannelPlugin
        plugin = OctoChannelPlugin()
        hooks.register(BEFORE_AGENT_RUN, plugin._on_agent_run)

        from ftre.config import AgentConfig
        ctx = AgentRunContext(
            session_id="sess_1",
            channel_id="octo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
            config=AgentConfig(),
        )
        result = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

        system_msg = result.messages[0]
        assert system_msg["role"] == "system"
        assert "Octo" in system_msg["content"]

    def test_hook_inserts_system_message_when_none_exists(self):
        """如果没有 system 消息，应插入一条新的"""
        from ftre.plugin import AgentRunContext, BEFORE_AGENT_RUN, HookManager
        hooks = HookManager()
        from octo_channel import OctoChannelPlugin
        plugin = OctoChannelPlugin()
        hooks.register(BEFORE_AGENT_RUN, plugin._on_agent_run)

        from ftre.config import AgentConfig
        ctx = AgentRunContext(
            session_id="sess_1",
            channel_id="octo",
            messages=[
                {"role": "user", "content": "Hello"},
            ],
            config=AgentConfig(),
        )
        result = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

        assert result.messages[0]["role"] == "system"
        assert "Octo" in result.messages[0]["content"]

    def test_hook_skips_non_octo_channels(self):
        """非 octo channel 的消息不应注入提示"""
        from ftre.plugin import AgentRunContext, BEFORE_AGENT_RUN, HookManager
        hooks = HookManager()
        from octo_channel import OctoChannelPlugin
        plugin = OctoChannelPlugin()
        hooks.register(BEFORE_AGENT_RUN, plugin._on_agent_run)

        from ftre.config import AgentConfig
        ctx = AgentRunContext(
            session_id="sess_1",
            channel_id="ws",  # 非 octo
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
            config=AgentConfig(),
        )
        result = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

        # system 消息不应被修改
        assert result.messages[0]["content"] == "You are a helpful assistant."


class TestOctoChannelIntegration:
    """端到端：从 WS 入站到 Agent 回复出站的完整链路"""

    @pytest.mark.asyncio
    async def test_full_round_trip_ws_to_send(self):
        """模拟：WS 收到消息 → Channel._handle_message() → Channel.send() 回复"""
        from octo_channel import OctoChannel

        bus = MagicMock()
        bus.publish_inbound = AsyncMock()
        bus.publish_outbound = AsyncMock()

        config = {
            "bot_token": "bf_test",
            "api_url": "https://api.example.com",
            "ws_url": "wss://ws.example.com/ws",
        }
        ch = OctoChannel(config, bus)
        ch.api.send_message = AsyncMock(return_value={"message_id": "reply_1"})

        # Step 1: 模拟 WS 收到群聊消息（嵌套事件格式）
        ws_event = {
            "event_id": 102,
            "message": {
                "message_id": 1002,
                "from_uid": "uid_alice",
                "channel_id": "ch_group_1",
                "channel_type": 2,
                "payload": {"type": 1, "content": "Hello, check the weather"},
                "timestamp": 1719700000,
            },
        }
        await ch._handle_message(ws_event)

        # 验证 publish_inbound 被调用
        bus.publish_inbound.assert_called_once()
        inbound_msg = bus.publish_inbound.call_args[0][0]
        assert inbound_msg.type == "user_message"
        assert inbound_msg.data["content"] == "Hello, check the weather"
        assert inbound_msg.from_session == "octo_2_ch_group_1"

        # Step 2: 模拟 AgentLoop 处理后的 outbound
        outbound_msg = BusMessage(
            type="agent_event",
            from_channel="octo",
            from_session=inbound_msg.from_session,
            to_channel="octo",
            to_session=inbound_msg.from_session,
            data={
                "type": "assistant_message_complete",
                "data": {"content": "Today sunny, 25C"},
            },
        )
        await ch.send(outbound_msg)

        # 验证 send_message 被调用，参数正确
        ch.api.send_message.assert_called_once()
        call_kwargs = ch.api.send_message.call_args[1]
        assert "Today sunny" in call_kwargs["content"]
        assert call_kwargs["channel_type"] == 2
        assert call_kwargs["channel_id"] == "ch_group_1"
