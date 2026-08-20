# Plugin compatibility boundary

The active backend runtime is `ftre.platform.plugin_runtime` over the public
`cordis` Context/Fiber/Service/Inject/Effect surface.  `ftre.plugin.kernel` is
only a migration compatibility API for pre-F1 tests and installed plugins; no
new Feature or Service may depend on it.

External plugins use an explicit `module:attribute` entry and are imported only
after configuration enables them.  The legacy `module.Class` spelling is
accepted temporarily and emits the same diagnostic path.

