"""F14.8 最小 Composition 与可选 Plugin 生命周期矩阵。

这里不调用真实 LLM，而是验证 Composition 的真实 Provider/Fiber 图：缺失可选
Package 不阻塞 Agent，卸载行为 Plugin 的 Hook/Route 会消失，关闭可重复调用。
长时间的模型/网络故障由各自契约测试覆盖，不用 sleep 掩盖并发竞态。
"""

from __future__ import annotations

import pytest

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_default_package_set_runs_with_compaction_enabled(tmp_path) -> None:
    """默认 Package 组合包含 Compaction，并仍能暴露 Agent Service。"""
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [
                {"id": "mcp", "disabled": True},
                {"id": "skill", "disabled": True},
                {"id": "plan", "disabled": True},
                {"id": "schedule", "disabled": True},
                {"id": "team", "disabled": True},
                {"id": "session-title", "disabled": True},
            ],
        }
    )
    try:
        statuses = {item.id: item for item in composition.plugins.statuses()}
        assert statuses["agents"].state.name == "ACTIVE"
        assert composition.context.get("agents") is not None
        assert composition.context.get("inbox") is not None
        assert statuses["compaction"].state.name == "ACTIVE"
        commands = composition.context.get("commands").list()
        assert {item["command"] for item in commands} >= {"/compact", "/compress-fast"}
    finally:
        await composition.close()
        await composition.close()


@pytest.mark.asyncio
async def test_business_package_can_still_be_disabled(tmp_path) -> None:
    """默认安装不等于强制执行：业务 Package 仍可通过配置禁用并清理自身行为。"""
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [{"id": "compaction", "enabled": False}],
        }
    )
    try:
        statuses = {item.id: item for item in composition.plugins.statuses()}
        assert statuses["agents"].state.name == "ACTIVE"
        assert "compaction" not in statuses
        commands = composition.context.get("commands").list()
        assert not {"/compact", "/compress-fast"}.intersection(
            item["command"] for item in commands
        )
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_builtin_plugin_unload_removes_its_hook_and_route(tmp_path) -> None:
    """可选行为卸载后不残留 Hook/Route，基础 Agent 仍保持可用。"""
    composition = await build_composition({"sessions_dir": str(tmp_path / "sessions")})
    try:
        hooks = composition.context.get("hook_runtime")
        assert any(item.owner == "session-title" for item in hooks.snapshot())
        assert await composition.plugins.unload("session-title") is True
        assert not [item for item in hooks.snapshot() if item.owner == "session-title" and not item.disposed]
        assert composition.context.get("agents") is not None
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_llm_recovery_plugin_restart_and_unload_are_reversible(tmp_path) -> None:
    """LLM 错误策略只贡献 Hook；重启/卸载不能留下旧 listener。"""
    composition = await build_composition({"sessions_dir": str(tmp_path / "sessions")})
    try:
        hooks = composition.context.get("hook_runtime")
        assert any(item.owner == "ftre-llm-recovery" and not item.disposed for item in hooks.snapshot())
        assert await composition.plugins.restart("llm-recovery") is True
        active = [
            item for item in hooks.snapshot()
            if item.owner == "ftre-llm-recovery" and not item.disposed
        ]
        assert len(active) == 1
        assert await composition.plugins.unload("llm-recovery") is True
        assert not [
            item for item in hooks.snapshot()
            if item.owner == "ftre-llm-recovery" and not item.disposed
        ]
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_llm_fallback_plugin_restart_and_unload_are_reversible(tmp_path) -> None:
    """Fallback 只贡献 llm/stream listener，生命周期清理与 recovery 对称。"""
    composition = await build_composition({"sessions_dir": str(tmp_path / "sessions")})
    try:
        hooks = composition.context.get("hook_runtime")
        active = [
            item for item in hooks.snapshot("llm/stream")
            if item.owner == "ftre-llm-fallback" and not item.disposed
        ]
        assert len(active) == 1
        assert await composition.plugins.restart("llm-fallback") is True
        active_after_restart = [
            item for item in hooks.snapshot("llm/stream")
            if item.owner == "ftre-llm-fallback" and not item.disposed
        ]
        assert len(active_after_restart) == 1
        assert await composition.plugins.unload("llm-fallback") is True
        assert not [
            item for item in hooks.snapshot("llm/stream")
            if item.owner == "ftre-llm-fallback" and not item.disposed
        ]
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_llm_fallback_can_be_disabled_without_affecting_agent(tmp_path) -> None:
    """禁用可选 fallback 后 Host 仍保留 Core 直连和 Agent Service。"""
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [{"id": "llm-fallback", "enabled": False}],
        }
    )
    try:
        statuses = {item.id: item for item in composition.plugins.statuses()}
        assert "llm-fallback" not in statuses
        assert composition.context.get("agents") is not None
        hooks = composition.context.get("hook_runtime")
        assert not [
            item for item in hooks.snapshot("llm/stream")
            if item.owner == "ftre-llm-fallback" and not item.disposed
        ]
    finally:
        await composition.close()
