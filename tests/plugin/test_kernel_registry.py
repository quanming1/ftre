import pytest

from ftre.plugin.kernel import (
    INTERNAL_PLUGIN_STATUS,
    FtreContext,
    Plugin,
    PluginDependencyCycleError,
    PluginRegistry,
    PluginState,
    ServiceAccessError,
)


@pytest.mark.asyncio
async def test_pending_plugin_activates_after_declared_service_is_provided():
    transitions = []
    ctx = FtreContext()
    ctx.on(
        INTERNAL_PLUGIN_STATUS,
        lambda instance, old, new: transitions.append((instance.name, new)),
    )
    registry = PluginRegistry(ctx)

    class Consumer(Plugin):
        name = "consumer"
        inject = ("answer",)

        async def setup(self, plugin_ctx, config):
            assert plugin_ctx.answer == 42

    consumer = await registry.register(Consumer)
    assert consumer.state is PluginState.PENDING

    ctx.provide("answer", 42)
    await registry.drain()
    assert consumer.state is PluginState.ACTIVE
    assert ("consumer", PluginState.LOADING) in transitions
    assert transitions[-1] == ("consumer", PluginState.ACTIVE)


@pytest.mark.asyncio
async def test_provider_dependency_order_and_cycle_detection():
    ctx = FtreContext()
    registry = PluginRegistry(ctx)
    order = []

    class Consumer(Plugin):
        name = "consumer"
        inject = ("clock",)

        async def setup(self, plugin_ctx, config):
            order.append(plugin_ctx.clock)

    class Provider(Plugin):
        name = "provider"
        provide = "clock"

        async def setup(self, plugin_ctx, config):
            plugin_ctx.provide("clock", "ready")

    await registry.register(Consumer)
    await registry.register(Provider)
    await registry.drain()
    assert order == ["ready"]

    cycle_ctx = FtreContext()
    cycle_registry = PluginRegistry(cycle_ctx)

    class A(Plugin):
        name = "a"
        inject = ("b",)
        provide = "a"

    class B(Plugin):
        name = "b"
        inject = ("a",)
        provide = "b"

    await cycle_registry.register(A)
    with pytest.raises(PluginDependencyCycleError, match="a.*b.*a"):
        await cycle_registry.register(B)


@pytest.mark.asyncio
async def test_undeclared_service_access_is_rejected():
    ctx = FtreContext()
    ctx.provide("secret", object())
    registry = PluginRegistry(ctx)

    class Intruder(Plugin):
        name = "intruder"

        async def setup(self, plugin_ctx, config):
            plugin_ctx.get("secret")

    instance = await registry.register(Intruder)
    assert instance.state is PluginState.FAILED
    assert isinstance(instance.error, ServiceAccessError)


@pytest.mark.asyncio
async def test_context_use_registers_nested_plugin_without_deadlock_and_cleans_it_up():
    ctx = FtreContext()
    registry = PluginRegistry(ctx)
    calls = []

    class Child(Plugin):
        name = "child"

        async def setup(self, plugin_ctx, config):
            calls.append("child:setup")
            return lambda: calls.append("child:cleanup")

    class Parent(Plugin):
        name = "parent"

        async def setup(self, plugin_ctx, config):
            calls.append("parent:setup")
            await plugin_ctx.use(Child)
            return lambda: calls.append("parent:cleanup")

    parent = await ctx.use(Parent)
    await registry.drain()

    assert parent.state is PluginState.ACTIVE
    assert registry.instances["child"].state is PluginState.ACTIVE
    assert calls[:2] == ["parent:setup", "child:setup"]

    await registry.unload("parent")
    assert "child" not in registry.instances
    assert calls[-2:] == ["parent:cleanup", "child:cleanup"]
