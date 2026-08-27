"""F33 Runtime Package 生命周期测试。

验证 Runtime Plugin 在真实 Composition 中的装配与卸载（AC4/AC12/AC14 的
生命周期侧面）：agents Service 与 Runtime Factory 由各自 entry point 装载、关闭后解绑、
重复关闭安全，以及洁净环境导入（AC16 的导入侧面）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]


@pytest.mark.asyncio
async def test_runtime_plugin_composes_binds_and_detaches(tmp_path) -> None:
    from ftre.app.gateway.composition import build_composition

    composition = await build_composition(
        {"sessions_dir": str(tmp_path / "sessions")}
    )
    agents = composition.context.get("agents")
    assert agents is not None
    assert agents.is_ready()
    assert agents.factory_name == "ftre-agent-runtime"
    # Runtime 通过 Service 转发状态；具体 Loop 不从 Service 公共面暴露。
    assert agents.get_session_status("nope") == "idle"

    await composition.close()

    assert not agents.is_ready()
    # 关闭后未绑定状态：查询回 idle、不抛异常。
    assert agents.status("nope") == "idle"


@pytest.mark.asyncio
async def test_composition_close_is_idempotent_for_runtime(tmp_path) -> None:
    from ftre.app.gateway.composition import build_composition

    composition = await build_composition(
        {"sessions_dir": str(tmp_path / "sessions")}
    )
    agents = composition.context.get("agents")
    await composition.close()
    await composition.close()  # 重复关闭必须幂等
    assert agents.status("x") == "idle"


def test_runtime_package_imports_in_clean_interpreter() -> None:
    """AC16 导入侧面：Runtime 包可在不挂载 ftre Host 源码的解释器中导入。

    依赖（ftre_agent、ftre_agent_core、ftre_llm、cordis）以独立安装后的
    位置解析：契约包 src 与 Runtime src 同时可见，ftre Host 源码不可见。
    """
    agent_src = ROOT / "packages" / "ftre-agent" / "src"
    runtime_src = ROOT / "packages" / "ftre-agent-runtime" / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, r'{agent_src}'); "
        f"sys.path.insert(0, r'{runtime_src}'); "
        "from ftre_agent_runtime import apply, AgentLoop, TurnExecutor; "
        "assert callable(apply); "
        "assert 'ftre.services' not in sys.modules and 'ftre' not in sys.modules; "
        "print('runtime package ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "runtime package ok" in result.stdout


def test_agent_hook_specs_dispatch_through_kernel_runtime() -> None:
    """Agent Hook spec 与 kernel HookRuntime 兼容：AGENT scope dispatch 需要隔离上下文。"""
    from cordis import Context
    from ftre_agent import (
        AGENT_BEFORE_RUN_SPEC,
        AgentSubject,
        AllowRun,
        BeforeRunPayload,
    )

    from ftre.kernel.hooks import HookRuntime

    runtime = HookRuntime(Context())
    registry = __import__("ftre_agent").AgentRegistry()
    registry.ensure("default")

    async def _run() -> None:
        payload = BeforeRunPayload(
            agent=AgentSubject("default", registry.scope_identity("default")),
            session_id="s",
            turn_id="t",
            cancellation=__import__("asyncio").Event(),
        )
        context = runtime.context_for_scope(registry.scope_carrier("default"))
        result = await runtime.dispatch(AGENT_BEFORE_RUN_SPEC, payload, context=context)
        assert isinstance(result, AllowRun)

    import asyncio

    asyncio.run(_run())
