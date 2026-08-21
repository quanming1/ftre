# platform

Owner: ftre runtime integration.  This layer adapts the public `cordis`
Context/Fiber/Service/Inject/Effect primitives and does not implement Session,
Agent, Tool or Feature business rules.

The public plugin runtime lives in `plugin_runtime`; it owns manifest metadata,
candidate discovery, explicit enablement, lifecycle diagnostics and cleanup.

