"""The single default Composition Root for ftre's backend."""
# 后端唯一的 Composition Root（组合根）：全进程只有这里负责"把零件组装成完整应用"。
# 承担三件事：
#   1. default_manifests() —— 声明内置 Plugin 清单（零件清单 + 装载顺序）；
#   2. build_composition() —— 执行组装：建 Context、种骨架服务、装载 Plugin；
#   3. Composition 类      —— 组装产物句柄：对外只暴露只读诊断与逆序关停。
#
# 边界约定：
#   - main.py / bootstrap.py 不构造业务对象，装配只发生在这里；
#   - 新增内置能力先加清单（default_manifests），再写对应测试；
#   - 任何 Plugin 的注册/路由/事件/后台任务都必须绑定 ctx.effect，保证 unload 可逆。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cordis import Context

from ftre.platform.hooks import HookRuntime
from ftre.platform.plugin_runtime import PluginManager, PluginManifest
from ftre.services.config.loader import load_config_file


def default_manifests() -> list[PluginManifest]:
    """Return the single ordered built-in catalog used by every Gateway host."""
    # 返回唯一的内置 Plugin 目录：每个 Gateway 宿主（CLI/测试/嵌入式）都用同一份清单。
    #
    # PluginManifest 位置参数：id, entry, source, required, default_enabled, description
    #   - required=True：必选 Plugin，启动失败会阻止 Gateway 启动（fail loud）；
    #   - required=False：可选 Plugin（features/*），失败只记录状态、不阻塞启动；
    #   - 顺序即装载顺序：先 services（核心能力），后 features（产品行为），
    #     因为 features 依赖 services 提供的 Service key。
    return [
        # ── 基础服务层（services，全部必选）──
        # 按依赖顺序排列：config 是配置事实源，filesystem 是 IO 策略，
        # http 提供路由注册表，其余服务依次建立在它们之上。
        PluginManifest("config", "ftre.services.config.plugin:apply", "builtin", True, True, description="root configuration"),
        PluginManifest("filesystem", "ftre.services.filesystem.plugin:apply", "builtin", True, True, description="path policy and atomic IO"),
        PluginManifest("http-service", "ftre.services.http.plugin:apply", "builtin", True, True, description="route contribution registry"),
        PluginManifest("system-prompt", "ftre.services.system_prompt.plugin:apply", "builtin", True, True, description="prompt section registry"),
        PluginManifest("message-bus", "ftre.services.messaging.bus.plugin:apply", "builtin", True, True, description="business message plane"),
        PluginManifest("tools", "ftre.services.tools.plugin:apply", "builtin", True, True, description="scoped tool registry"),
        PluginManifest("agent-profiles", "ftre.services.agent.profile.plugin:apply", "builtin", True, True, description="agent profile merge"),
        PluginManifest("agents", "ftre.services.agent.plugin:apply", "builtin", True, True, description="public agent facade"),
        PluginManifest("sessions", "ftre.services.session.plugin:apply", "builtin", True, True, description="session persistence facade"),
        PluginManifest("commands", "ftre.services.command.plugin:apply", "builtin", True, True, description="command registry"),
        PluginManifest("workspaces", "ftre.services.workspace.plugin:apply", "builtin", True, True, description="workspace boundary"),
        PluginManifest("channels", "ftre.services.messaging.channel.plugin:apply", "builtin", True, True, description="channel registry"),
        PluginManifest("attachments", "ftre.services.attachment.plugin:apply", "builtin", True, True, description="attachment storage"),
        PluginManifest("traces", "ftre.services.observability.trace.plugin:apply", "builtin", True, True, description="trace persistence"),
        # 可选独立队列包：未安装时 AgentService 仍可直接执行 InboundMessage；
        # 安装后由该 Package 接管 pending、worker 和 session queue 协议。
        PluginManifest("agent-runtime", "ftre.services.agent_loop.plugin:apply", "builtin", True, True, description="agent execution runtime"),
        PluginManifest("inbox", "ftre_inbox.plugin:apply", "builtin", False, True, description="optional durable inbox package"),
        PluginManifest("subagent-channel", "ftre.services.messaging.channel.providers.subagent.plugin:apply", "builtin", True, True, description="internal subagent channel"),
        PluginManifest("websocket-channel", "ftre.services.messaging.channel.providers.websocket.plugin:apply", "builtin", True, True, description="desktop WebSocket channel"),
        # ── 产品行为层（features / 可选）──
        # 这些 Plugin 只消费上面的公开 Service key，不访问私有实现；
        # required=False 表示能力缺失（如 MCP 未配置）不应阻止 Gateway 启动。
        PluginManifest("skill", "ftre.features.skill.plugin:apply", "builtin", False, True, description="skill catalog and tool"),
        PluginManifest("mcp", "ftre.features.mcp.plugin:apply", "builtin", False, True, description="MCP connection state"),
        PluginManifest("plan", "ftre.features.plan.plugin:apply", "builtin", False, True, description="plan behavior"),
        PluginManifest("team", "ftre.features.team.plugin:apply", "builtin", False, True, description="team lifecycle"),
        PluginManifest("schedule", "ftre.features.schedule.plugin:apply", "builtin", False, True, description="cron persistence"),
        PluginManifest("context-govern", "ftre.features.context_govern.plugin:apply", "builtin", True, True, description="workspace governance"),
        PluginManifest("session-title", "ftre.services.session.title.plugin:apply", "builtin", False, True, description="title behavior"),
    ]


@dataclass
class Composition:
    """Own the Context, PluginManager, config snapshot, and optional HTTP app."""
    # 组装产物的句柄，持有四样东西：
    #   - context:  运行期 Service 注册表（ctx 上有什么，看这里被塞了什么）；
    #   - plugins:  PluginManager（Fiber 生命周期、诊断、逆序关闭）；
    #   - config:   本次启动的配置快照；
    #   - http_app: 可选，物化后的 FastAPI 实例（start_gateway 才填充）。
    # 对外只暴露 diagnostics（只读）与 close（逆序释放），不暴露内部可变细节。
    context: Context
    plugins: PluginManager
    config: dict[str, Any]
    http_app: Any | None = None

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        """Return a JSON-ready snapshot without exposing PluginManager internals."""
        # 把各 Fiber 的状态（ACTIVE/PENDING/FAILED、依赖注入、effect 数量等）
        # 转成 JSON 友好列表供诊断 API 使用；不把 PluginManager 本体暴露出去。
        return self.plugins.diagnostics()

    async def close(self) -> None:
        """Dispose all Plugin Fibers in reverse lifecycle order."""
        # 逆序释放：后装载的 Plugin 先关，保证"谁后建谁先关"；
        # 每个 Fiber 的 effect 清理（路由摘除、任务取消、连接关闭）随 Fiber 一并
        # 执行，且幂等可重复调用。
        await self.plugins.close()

async def build_composition(
    config_data: dict[str, Any] | None = None,
    *,
    plugins_dir=None,
    initial_services: dict[str, Any] | None = None,
) -> Composition:
    """Create and settle a composition without opening a listening socket."""
    # 执行组装（唯一的组装点），不打开监听端口——监听由 bootstrap/宿主决定。
    #
    # 步骤：
    #   1. 加载配置（外部传入或从 ~/.ftre/config.json 读取）；
    #   2. 建根 Context：所有 Service 注册到这一个容器，保证全进程单实例；
    #   3. 种下 hook_runtime 骨架服务（必须先于任何 Plugin，见下方注释）；
    #   4. 注入 initial_services（bootstrap/测试预置的服务，Plugin 同名不重复创建）；
    #   5. 装载 default_manifests() 全部 Plugin（含外部插件目录扫描）；
    #   6. 返回 Composition 句柄（路由由各自 Plugin 在 Fiber 内贡献）。
    config = config_data if config_data is not None else load_config_file()
    context = Context()
    # One HookRuntime per Composition lets Plugins register through their Fiber
    # while AgentLoop/Session/Tool adapters share the same Cordis event graph.
    # 每个 Composition 只创建一个 HookRuntime 并直接种在 Context 根部：
    # Plugin 通过 ctx.hook_runtime.register() 挂语义 Hook，若 HookRuntime 自己
    # 也是 Plugin，就会出现"Plugin 要注册 Hook 但提供 HookRuntime 的 Plugin
    # 还没装载"的初始化顺序死锁。它是唯一由 Composition 直接提供的骨架服务。
    context.provide("hook_runtime", HookRuntime(context))
    # initial_services：预置服务（bootstrap 或测试注入），同名 key 不再由 Plugin 覆盖
    for name, value in (initial_services or {}).items():
        if value is not None:
            context.provide(name, value)
    # 装载全部内置 Plugin + 扫描外部插件目录；config 作为装载参数传给每个 apply()
    manager = PluginManager(context, plugins_dir=plugins_dir)
    # PluginManager is itself a kernel capability.  The runtime Provider Plugin
    # receives this same manager through Context instead of bootstrap wiring it.
    context.provide("plugin_manager", manager)
    await manager.load(default_manifests(), config)
    # 组装完成：封装句柄；路由已由各自 Provider/Feature Plugin 贡献。
    composition = Composition(context=context, plugins=manager, config=config)
    return composition
