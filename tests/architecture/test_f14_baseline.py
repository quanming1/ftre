"""F14.1 架构基线门禁。

这些断言记录 F14 开工时真实存在的装配约束。它们不把尚未迁移的旧目录
伪装成目标状态，而是保证后续切片不能新增重复 Owner、非法入口或 Package
反向依赖；目录迁移完成后再由对应批次收紧路径断言。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from ftre.app.gateway.composition import default_manifests

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
PACKAGES = ROOT / "packages"


def _literal_names(path: Path, name: str) -> tuple[str, ...]:
    """Read declarative ``inject``/``provide`` values without importing a Plugin."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List)):
            return ()
        return tuple(
            item.value
            for item in value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return ()


def _plugin_files() -> list[Path]:
    return [
        *sorted((SRC / "services").rglob("plugin.py")),
        *sorted((SRC / "plugins" / "builtin").rglob("plugin.py")),
        *sorted(PACKAGES.glob("*/src/*/plugin.py")),
    ]


def test_default_manifest_ids_and_entries_are_unique_and_resolvable() -> None:
    """默认 Composition 必须只有一份清单，且每个入口能解析到 apply。"""
    manifests = default_manifests()
    ids = [manifest.id for manifest in manifests]
    assert len(ids) == len(set(ids))
    for manifest in manifests:
        assert ":" in manifest.entry_text, manifest.id
        module_name, _, attribute = manifest.entry_text.partition(":")
        assert module_name and attribute, manifest.id
        target = getattr(importlib.import_module(module_name), attribute)
        assert callable(target) or hasattr(target, "apply"), manifest.id


def test_builtin_provider_keys_have_one_source_owner() -> None:
    """同一个 provide key 不能由两个源码 Plugin 同时声明。"""
    owners: dict[str, Path] = {}
    for path in _plugin_files():
        for key in _literal_names(path, "provide"):
            previous = owners.setdefault(key, path)
            assert previous == path, f"duplicate provide owner {key!r}: {previous} / {path}"


def test_host_service_keys_have_declared_provider_owners() -> None:
    """Host Service 表必须能反查到唯一 Provider，而不是依赖隐藏手工装配。"""
    expected = {
        "config": "config",
        "filesystem": "filesystem",
        "http": "http-service",
        "system_prompt": "system-prompt",
        "message_bus": "message-bus",
        "tools": "tools",
        "agent_profiles": "agent-profiles",
        "sessions": "sessions",
        "agents": "agents",
        "channels": "channels",
        "workspaces": "workspaces",
        "attachments": "attachments",
    }
    manifests = {manifest.id: manifest for manifest in default_manifests()}
    providers: dict[str, list[Path]] = {}
    for path in _plugin_files():
        for key in _literal_names(path, "provide"):
            providers.setdefault(key, []).append(path)
    for key, owner_id in expected.items():
        assert owner_id in manifests, (key, owner_id)
        assert len(providers.get(key, [])) == 1, key
        module_name = manifests[owner_id].entry_text.partition(":")[0]
        module = importlib.import_module(module_name)
        assert Path(module.__file__).resolve() == providers[key][0].resolve()


def test_plugin_declarations_are_literal_and_have_stable_entry_shape() -> None:
    """Plugin 元数据必须可静态审计，不能靠运行时动态拼接依赖图。"""
    assert _plugin_files(), "expected builtin provider plugins"
    for path in _plugin_files():
        inject = _literal_names(path, "inject")
        provide = _literal_names(path, "provide")
        assert all(inject), path
        assert all(provide), path


def test_optional_runtime_capabilities_are_ready_before_agent_provider() -> None:
    """Agent Runtime 构造时必须能看到已声明的可选 MCP/Trace 能力。"""
    manifests = default_manifests()
    positions = {manifest.id: index for index, manifest in enumerate(manifests)}
    assert positions["mcp"] < positions["agents"]
    assert positions["traces"] < positions["agents"]
    assert next(manifest for manifest in manifests if manifest.id == "traces").required is False


def test_agent_runtime_delegates_session_events_to_the_session_owner() -> None:
    """Agent Runtime 不能保留 Projection/Hook/Bus 的第二事件出口。"""
    source = (
        ROOT
        / "packages"
        / "ftre-agent-runtime"
        / "src"
        / "ftre_agent_runtime"
        / "engine.py"
    ).read_text(encoding="utf-8")
    assert "self.session_events.emit(" in source
    assert "self.session_projection.apply(" not in source
    assert "_emit_session_event_hook" not in source
    assert "SESSION_EVENT_SPEC" not in source


def test_kernel_mechanism_layer_is_business_free_at_baseline() -> None:
    """当前 platform 机制层不应认识产品 Service 或可选 Package。"""
    roots = [SRC / "kernel" / "hooks", SRC / "kernel" / "plugins"]
    forbidden = (
        "ftre.services",
        "ftre.plugins.builtin",
        "ftre_inbox",
        "CompactionService",
        "CommandService",
        "QueueItem",
        "restart_required",
        "websocket",
    )
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not any(item in source for item in forbidden), path


def test_packages_do_not_import_host_private_runtime() -> None:
    """独立 Package 只能依赖公开边界，不能反向拿 Host 私有实现。"""
    forbidden = (
        "ftre.services.agent_loop",
        "ftre.services.session.persistence",
        "ftre.services.agent.runtime",
        "ftre.app.gateway",
    )
    for path in PACKAGES.glob("*/src/**/*.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(item in source for item in forbidden), path


def test_known_legacy_escape_hatches_are_not_reintroduced() -> None:
    """F14 后续切片不能重新引入已退役的双 Owner 入口。"""
    forbidden = ("bind_legacy", "ServiceBag", "AgentControlPort", "CompactionPort")
    roots = (SRC, *(package / "src" for package in PACKAGES.iterdir() if package.is_dir()))
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not any(item in source for item in forbidden), path


def test_runtime_services_do_not_expose_callback_setter_bridges() -> None:
    """跨 Owner 协作必须在构造/Inject/Hook 边界完成，不能靠 bind setter。"""
    forbidden = (
        "def bind_",
        ".bind_lifecycle(",
        ".bind_hook_runtime(",
        ".bind_flush_dispatcher(",
        "session_event_unbind",
        "_flush_unbind",
    )
    roots = (SRC, *(package / "src" for package in PACKAGES.iterdir() if package.is_dir()))
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not any(item in source for item in forbidden), path


def test_builtin_plugin_tree_has_no_legacy_feature_or_adapter_roots() -> None:
    """F14.5 makes Plugin ownership visible in the filesystem itself."""
    for retired in (
        SRC / "features",
        SRC / "services" / "command",
        SRC / "services" / "observability",
        SRC / "services" / "session" / "title",
        SRC / "services" / "messaging" / "channel" / "providers",
    ):
        assert not retired.exists(), retired
    for expected in (
        SRC / "plugins" / "builtin" / "command" / "plugin.py",
        SRC / "plugins" / "builtin" / "trace" / "plugin.py",
        SRC / "plugins" / "builtin" / "session_title" / "plugin.py",
        SRC / "plugins" / "builtin" / "channels" / "websocket" / "plugin.py",
        SRC / "plugins" / "builtin" / "channels" / "subagent" / "plugin.py",
    ):
        assert expected.is_file(), expected


def test_websocket_channel_has_no_post_composition_dependency_setters() -> None:
    """Concrete Channel 的依赖必须在同一 Plugin 构造时确定。

    这条门禁专门防止 Bootstrap 或 Plugin 先创建半成品 Channel，再用
    ``attach_*``/``set_*`` 回填 projection、Inbox、状态和 FastAPI App；
    WebSocket handler 现在作为 HttpService 路由贡献一起物化。
    """
    channel = (SRC / "plugins" / "builtin" / "channels" / "websocket" / "channel.py").read_text(
        encoding="utf-8"
    )
    plugin = (SRC / "plugins" / "builtin" / "channels" / "websocket" / "plugin.py").read_text(
        encoding="utf-8"
    )
    bootstrap = (SRC / "app" / "gateway" / "bootstrap.py").read_text(encoding="utf-8")
    forbidden = (
        "def set_session_projection",
        "def set_inbox_provider",
        "def set_status_provider",
        "def attach_app",
        ".attach_app(",
        ".set_session_projection(",
        ".set_inbox_provider(",
        ".set_status_provider(",
    )
    assert not any(item in channel or item in plugin or item in bootstrap for item in forbidden)
    assert "register_websocket_path(" in plugin
    assert "channel._ws_endpoint" in plugin
