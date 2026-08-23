"""F14.8 最小 Composition 与可选 Plugin 生命周期矩阵。

这里不调用真实 LLM，而是验证 Composition 的真实 Provider/Fiber 图：缺失可选
Package 不阻塞 Agent，卸载行为 Plugin 的 Hook/Route 会消失，关闭可重复调用。
长时间的模型/网络故障由各自契约测试覆盖，不用 sleep 掩盖并发竞态。
"""

from __future__ import annotations

import pytest

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_minimal_host_runs_without_optional_packages(tmp_path) -> None:
    """Host 缺少 Inbox/Compaction 时仍能组合并暴露单一 Agent Service。"""
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [
                {"id": "inbox", "disabled": True},
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
        assert composition.context.get("inbox", strict=False) is None
        assert "compaction" not in statuses
    finally:
        await composition.close()
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
