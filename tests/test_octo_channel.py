import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from ftre.bus import BusMessage


class TestExtractParentGroupNo:
    """extract_parent_group_no 工具函数测试"""

    def test_plain_group_no_returns_unchanged(self):
        from octo_channel import extract_parent_group_no
        assert extract_parent_group_no("fb924c042aee4cd6b055ca61ac340093") == "fb924c042aee4cd6b055ca61ac340093"

    def test_thread_compound_extracts_group_no(self):
        from octo_channel import extract_parent_group_no
        assert extract_parent_group_no("fb924c042aee4cd6b055ca61ac340093____2064912548183937024") == "fb924c042aee4cd6b055ca61ac340093"

    def test_empty_string_returns_empty(self):
        from octo_channel import extract_parent_group_no
        assert extract_parent_group_no("") == ""


class FakeExternalSessionManager:
    def __init__(self):
        self.session_id = "octo::sess_mapped"
        self.get_or_create_calls = []
        self.external_data = {
            "channel_type": 2,
            "channel_id": "ch_group_1",
            "from_uid": "uid_alice",
        }

    async def get_or_create_external_session(self, **kwargs):
        self.get_or_create_calls.append(kwargs)
        self.external_data = kwargs["external_data"]
        return self.session_id

    async def get_external_session(self, session_id):
        if session_id != self.session_id:
            return None
        return {
            "channel_id": "octo",
            "external_key": "octo:2:ch_group_1",
            "session_id": self.session_id,
            "external_data": self.external_data,
        }


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
            "robot_id": "bot_123",
            "im_token": "im_test_token",
            "ws_url": "wss://ws.example.com/ws",
            "owner_uid": "uid_owner",
        })
        mock_resp.__aenter__.return_value = mock_resp
        mock_session.post = MagicMock(return_value=mock_resp)
        api._session = mock_session

        result = await api.register_bot()

        assert result["robot_id"] == "bot_123"
        assert result["im_token"] == "im_test_token"
        assert result["ws_url"] == "wss://ws.example.com/ws"
        call_args = mock_session.post.call_args
        assert "/v1/bot/register" in call_args[0][0]

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
            "require_mention": False,  # 测试默认关闭 @ 检测，专门的门控测试在 TestOctoMentionGate 中
        }

    def _make_channel(self, config, bus, **kwargs):
        """创建 OctoChannel 实例并 mock 掉所有外部 API 调用。"""
        from octo_channel import OctoChannel
        ch = OctoChannel(config, bus, **kwargs)
        ch.api.get_channel_messages = AsyncMock(return_value=[])
        ch.api.get_group_members = AsyncMock(return_value=[])
        return ch

    @pytest.mark.asyncio
    @patch("octo_channel.subprocess.Popen")
    @patch("octo_channel.aiohttp.ClientSession")
    async def test_start_launches_bridge_and_connects(self, mock_session_cls, mock_popen, mock_bus, channel_config):
        """start() should launch bridge subprocess and connect to local WS"""
        from octo_channel import OctoChannel

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # process is still alive
        mock_popen.return_value = mock_proc

        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        mock_session_cls.return_value = mock_session

        ch = OctoChannel(channel_config, mock_bus)
        ch.api.register_bot = AsyncMock(return_value={"robot_id": "bot_123"})

        await ch.start()

        # 验证启动了桥接进程
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        # call_args 格式：((args_list,), {kwargs})
        args_list = call_args[0][0] if call_args[0] else call_args[1].get('args', [])
        assert "node" in args_list[0]
        assert any("octo-bridge.js" in arg for arg in args_list)

        # 验证连接了本地桥接 WS
        mock_session.ws_connect.assert_called_once_with("ws://127.0.0.1:9876")
        assert ch._bot_uid == "bot_123"

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
        """WS TEXT frame (flat WuKongIM format) should parse to user_message and call receive()"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        # WuKongIM 扁平格式（桥接已解密）
        msg = {
            "message_id": "1002",
            "message_seq": 42,
            "from_uid": "uid_alice",
            "channel_id": "ch_group_1",
            "channel_type": 2,
            "timestamp": 1719700000,
            "payload": {"type": 1, "content": "Hello bot"},
        }

        await ch._handle_message(msg)

        mock_bus.publish_inbound.assert_called_once()
        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.type == "user_message"
        assert call_msg.data["content"] == "[来自 uid_alice]: Hello bot"
        assert call_msg.data["from_uid"] == "uid_alice"
        assert call_msg.data["channel_type"] == 2
        # session_id 应编码 channel_type
        assert "octo_2_" in call_msg.from_session

    @pytest.mark.asyncio
    async def test_ws_message_uses_external_session_mapping(self, mock_bus, channel_config):
        from octo_channel import OctoChannel

        session_manager = FakeExternalSessionManager()
        ch = OctoChannel(channel_config, mock_bus, session_manager=session_manager)

        msg = {
            "message_id": "1002",
            "message_seq": 42,
            "from_uid": "uid_alice",
            "channel_id": "ch_group_1",
            "channel_type": 2,
            "timestamp": 1719700000,
            "payload": {"type": 1, "content": "Hello bot"},
        }

        await ch._handle_message(msg)

        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.from_session == "octo::sess_mapped"
        assert call_msg.data["session_id"] == "octo::sess_mapped"
        assert call_msg.data["octo_external_key"] == "octo:2:ch_group_1"
        assert session_manager.get_or_create_calls[0]["channel_id"] == "octo"
        assert session_manager.get_or_create_calls[0]["external_key"] == "octo:2:ch_group_1"

    @pytest.mark.asyncio
    async def test_skips_self_message(self, mock_bus, channel_config):
        from octo_channel import OctoChannel

        ch = OctoChannel({**channel_config, "bot_id": "bot_123"}, mock_bus)

        msg = {
            "message_id": "1003",
            "message_seq": 43,
            "from_uid": "bot_123",
            "channel_id": "uid_alice",
            "channel_type": 1,
            "timestamp": 1719700000,
            "payload": {"type": 1, "content": "self echo"},
        }

        await ch._handle_message(msg)

        mock_bus.publish_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_self_event_message(self, mock_bus, channel_config):
        from octo_channel import OctoChannel

        ch = OctoChannel({**channel_config, "bot_id": "bot_123"}, mock_bus)

        msg = {
            "message_id": "1004",
            "message_seq": 44,
            "from_uid": "bot_123",
            "channel_id": "uid_alice",
            "channel_type": 1,
            "timestamp": 1719700000,
            "payload": {
                "type": 1,
                "content": "event payload",
                "event": {"type": "group_md_updated"},
            },
        }

        await ch._handle_message(msg)

        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_ws_dm_event_uses_from_uid_as_channel(self, mock_bus, channel_config):
        """DM 事件无 channel_id，应使用 from_uid 作为 channel_id，channel_type=1"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        # DM 事件：无 channel_id / channel_type
        msg = {
            "message_id": "1001",
            "message_seq": 1,
            "from_uid": "uid_alice",
            "channel_id": "",
            "channel_type": 1,
            "timestamp": 1700000000,
            "payload": {"type": 1, "content": "Hi bot!"},
        }

        await ch._handle_message(msg)

        mock_bus.publish_inbound.assert_called_once()
        call_msg = mock_bus.publish_inbound.call_args[0][0]
        assert call_msg.data["channel_type"] == 1  # DM
        assert call_msg.data["channel_id"] == "uid_alice"
        assert call_msg.from_session == "octo_1_uid_alice"

    @pytest.mark.asyncio
    async def test_send_uses_external_session_mapping(self, mock_bus, channel_config):
        from octo_channel import OctoChannel

        session_manager = FakeExternalSessionManager()
        ch = OctoChannel(channel_config, mock_bus, session_manager=session_manager)
        ch.api.send_message = AsyncMock(return_value={"message_id": "msg_1"})

        msg = BusMessage(
            type="agent_event",
            from_channel="octo",
            from_session="octo::sess_mapped",
            to_channel="octo",
            to_session="octo::sess_mapped",
            data={
                "type": "assistant_message_complete",
                "data": {"content": "Hello from mapped session"},
            },
        )

        await ch.send(msg)

        ch.api.send_message.assert_called_once()
        call_kwargs = ch.api.send_message.call_args[1]
        assert call_kwargs["channel_type"] == 2
        assert call_kwargs["channel_id"] == "ch_group_1"
        assert call_kwargs["content"] == "Hello from mapped session"

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
            "require_mention": False,
        }
        ch = OctoChannel(config, bus)
        ch.api.send_message = AsyncMock(return_value={"message_id": "reply_1"})
        ch.api.get_channel_messages = AsyncMock(return_value=[])
        ch.api.get_group_members = AsyncMock(return_value=[])

        # Step 1: 模拟 WS 收到群聊消息（扁平 WuKongIM 格式）
        ws_msg = {
            "message_id": "1002",
            "message_seq": 42,
            "from_uid": "uid_alice",
            "channel_id": "ch_group_1",
            "channel_type": 2,
            "timestamp": 1719700000,
            "payload": {"type": 1, "content": "Hello, check the weather"},
        }
        await ch._handle_message(ws_msg)

        # 验证 publish_inbound 被调用
        bus.publish_inbound.assert_called_once()
        inbound_msg = bus.publish_inbound.call_args[0][0]
        assert inbound_msg.type == "user_message"
        assert "Hello, check the weather" in inbound_msg.data["content"]
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


class TestOctoMentionGate:
    """群聊 @ 检测门控测试"""

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
            "bot_id": "bot_self_001",
            "bot_name": "ftre开发",
            "require_mention": True,
        }

    def _make_channel(self, config, bus, **kwargs):
        """创建 OctoChannel 实例并 mock 掉所有外部 API 调用。"""
        from octo_channel import OctoChannel
        ch = OctoChannel(config, bus, **kwargs)
        ch.api.get_channel_messages = AsyncMock(return_value=[])
        ch.api.get_group_members = AsyncMock(return_value=[])
        return ch

    @pytest.mark.asyncio
    async def test_group_mentioned_by_uid_dispatches(self, mock_bus, channel_config):
        """群聊中直接 @bot（uids 包含 bot_uid）→ 应投递消息"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m1", "message_seq": 1,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "你好 @ftre开发",
                "mention": {"uids": ["bot_self_001"]},
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_mentioned_by_ais_dispatches(self, mock_bus, channel_config):
        """群聊中 @AI → 应投递消息"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m2", "message_seq": 2,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "这个问题谁会 @AI",
                "mention": {"ais": 1},
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_not_mentioned_skipped(self, mock_bus, channel_config):
        """群聊中未被 @ → 不应投递消息"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m3", "message_seq": 3,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "今天天气不错",
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_always_dispatches(self, mock_bus, channel_config):
        """私聊消息始终投递，不受 require_mention 影响"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m4", "message_seq": 4,
            "from_uid": "user_001", "channel_id": "",
            "channel_type": 1,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "你好",
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_require_mention_false_always_dispatches(self, mock_bus, channel_config):
        """require_mention=False 时群聊消息始终投递"""
        from octo_channel import OctoChannel

        config = {**channel_config, "require_mention": False}
        ch = OctoChannel(config, mock_bus)

        msg = {
            "message_id": "m5", "message_seq": 5,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "不用@也能回复",
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_self_message_always_skipped(self, mock_bus, channel_config):
        """自己的消息始终跳过（即使有 @）"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m6", "message_seq": 6,
            "from_uid": "bot_self_001",
            "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "我自己发的",
                "mention": {"uids": ["bot_self_001"]},
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_bot_mentioned_does_not_trigger(self, mock_bus, channel_config):
        """群聊中 @ 了其他 bot（非自己）→ 不应投递"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m7", "message_seq": 7,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "@other_bot 帮帮忙",
                "mention": {"uids": ["other_bot_999"]},
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_fallback_mention_detection(self, mock_bus, channel_config):
        """文本兜底：内容包含 @bot名称 但无 mention payload → 应投递"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m8", "message_seq": 8,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "@ftre开发 帮我看下这个问题",
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_text_fallback_no_match_without_at(self, mock_bus, channel_config):
        """文本兜底：内容包含 bot 名称但无 @ 前缀 → 不触发"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m9", "message_seq": 9,
            "from_uid": "user_001", "channel_id": "group_001",
            "channel_type": 2,
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "ftre开发 这个怎么搞",
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_not_called()

    # --- Thread（讨论串）门控测试 ---

    @pytest.mark.asyncio
    async def test_thread_mentioned_dispatches(self, mock_bus, channel_config):
        """讨论串中 @bot → 应投递消息"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m10", "message_seq": 10,
            "from_uid": "user_001", "channel_id": "thread_001",
            "channel_type": 5,  # Thread
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "@ftre开发 帮我看下",
                "mention": {"uids": ["bot_self_001"]},
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_thread_not_mentioned_skipped(self, mock_bus, channel_config):
        """讨论串中未被 @ → 不应投递消息"""
        from octo_channel import OctoChannel

        ch = OctoChannel(channel_config, mock_bus)

        msg = {
            "message_id": "m11", "message_seq": 11,
            "from_uid": "user_001", "channel_id": "thread_001",
            "channel_type": 5,  # Thread
            "timestamp": 1234567890,
            "payload": {
                "type": 1,
                "content": "今天天气不错",
            },
        }

        await ch._handle_message(msg)
        mock_bus.publish_inbound.assert_not_called()
