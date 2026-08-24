"""F19 Session Route Plugin 边界门禁。"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def test_session_provider_owns_data_not_http_routes() -> None:
    provider = (SRC / "services" / "session" / "plugin.py").read_text(encoding="utf-8")
    assert 'inject = ("hook_runtime", "message_bus")' in provider
    assert "register_router" not in provider
    assert 'ctx.get("agents"' not in provider
    assert 'ctx.get("inbox"' not in provider


def test_session_routes_plugin_declares_all_route_dependencies() -> None:
    plugin = (SRC / "plugins" / "builtin" / "session_routes" / "plugin.py").read_text(
        encoding="utf-8"
    )
    assert 'inject = ("sessions", "agents", "inbox", "http")' in plugin
    assert 'ctx.http.register_router' in plugin
    assert 'owner="sessions"' in plugin
    assert 'ctx.effect' in plugin


def test_session_router_has_no_late_service_accessor() -> None:
    router = (SRC / "services" / "session" / "router.py").read_text(encoding="utf-8")
    assert "def current(" not in router
    assert "callable(agents)" not in router
    assert "callable(inbox)" not in router
