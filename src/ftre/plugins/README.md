# plugins

`plugins/` contains complete product behaviors and concrete protocol adapters.
Each directory has one Plugin Owner, declares its `inject`/`provide` contract,
and binds every route, Hook, Tool, Channel, task and resource to a Cordis
Effect. A Plugin may provide a private Service, but it must not reach into
another Owner's repository or runtime.

`builtin/` means the ftre distribution maintains the Plugin and the default
Composition may enable it; it does not mean the capability bypasses Plugin
lifecycle. Capabilities that pass the independent installation gate live under
the repository-level `packages/` directory instead.
