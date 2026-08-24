<!-- 中文说明：kernel 层文档：解释 Hook 和 Plugin Runtime 如何提供生命周期基础设施，而不拥有产品数据。 -->

# kernel

Owner: ftre runtime integration.  This layer adapts the public `cordis`
Context/Fiber/Service/Inject/Effect primitives and does not implement Session,
Agent, Tool or Feature business rules.

The public plugin runtime lives in `plugins`; it owns manifest metadata,
candidate discovery, explicit enablement, lifecycle diagnostics and cleanup.
