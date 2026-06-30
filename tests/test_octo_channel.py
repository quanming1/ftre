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
            channel_type="group",
            content="Hello from ftre",
            im_token="im_test_token"
        )

        assert result["message_id"] == "msg_001"
        call_args = mock_session.post.call_args
        assert "/v1/bot/send_message" in call_args[0][0]


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
        """send() should extract text from BusMessage and call send_message"""
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
                "data": {"content": "Hello, I am ftre bot"},
            },
        )

        await ch.send(msg)

        ch.api.send_message.assert_called_once()
        call_kwargs = ch.api.send_message.call_args[1]
        assert "Hello, I am ftre bot" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_ws_text_frame_triggers_receive(self, mock_bus, channel_config):
        """WS TEXT frame should parse to user_message and call receive()"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)
        ch._im_token = "im_xxx"

        data = {
            "type": 1,
            "content": "Hello bot",
            "from_uid": "uid_alice",
            "channel_id": "ch_group_1",
            "channel_type": 1,
            "message_id": "msg_in_1",
            "timestamp": 1719700000,
        }

        await ch._handle_message(data)

        mock_bus.publish_inbound.assert_called_once()
        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.type == "user_message"
        assert call_msg.data["content"] == "Hello bot"
        assert call_msg.data["from_uid"] == "uid_alice"

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
    """Plugin hook 测试（占位，Task 3 实现）"""

    def test_hook_injects_octo_hint_into_system_message(self):
        pass

    def test_hook_inserts_system_message_when_none_exists(self):
        pass


class TestOctoChannelIntegration:
    """端到端集成测试（占位，Task 4 实现）"""

    @pytest.mark.asyncio
    async def test_full_round_trip_ws_to_send(self):
        pass
