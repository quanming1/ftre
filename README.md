# FTRE Gateway

English | [中文](README.zh-CN.md)

FTRE is a local-first AI coding assistant. This repository contains its stateful
Python Gateway: it composes runtime Services and Plugins, owns sessions and
agent execution, and exposes HTTP/WebSocket APIs to the desktop client.

The stateless algorithm layer lives in `ftre-agent-core`; the desktop and docs
projects are separate repositories and are outside the scope of this backend.

## Architecture at a glance

```text
CLI (ftre.main)
  └─ Gateway bootstrap
      └─ Composition Root (app/gateway/composition.py)
          └─ cordis Context / Fiber
              ├─ Platform: plugin discovery, loading, lifecycle diagnostics
              ├─ Services: stateful shared capabilities
              ├─ Features: product behavior Plugins
              └─ HTTP Host + Channels + Agent data plane
```

There is one Composition Root. It declares the built-in Plugin manifests,
applies configuration, creates the Cordis context, registers startup routes,
and owns the reversible shutdown path.

### Service, Provider Plugin, and Feature Plugin

| Concept | Location | Responsibility |
| --- | --- | --- |
| Service | `src/ftre/services/<name>/service.py` | Stateful capability with a stable public key, such as `sessions`, `tools`, `http` or `message_bus` |
| Provider Plugin | beside the Service in `plugin.py` | Declares `inject`/`provide`, creates or binds the Service, and registers cleanup effects |
| Feature Plugin | `src/ftre/features/<name>/` | Optional product behavior such as Skill, MCP, Plan, Team, Schedule or context governance |
| Platform Runtime | `src/ftre/platform/plugin_runtime/` | Manifest validation, explicit discovery, Cordis loading, status and failure diagnostics |
| App Host | `src/ftre/app/` | Process boundaries only: CLI, Gateway bootstrap, FastAPI and uvicorn |

`services/agent_loop/provider.py` is the internal object-construction boundary
for the Agent runtime. It is not a Service or Plugin entry point. Plugin
entries use `module:attribute` and normally point to an `apply(ctx, config)`
function.

## Repository tree

```text
ftre/
├─ pyproject.toml                 # Python package and cordis-py dependency
├─ config.example.json            # ~/.ftre/config.json template
├─ docs/                          # PRD, process, TODO and execution records
├─ src/
│  └─ ftre/
│     ├─ main.py                  # thin Typer entry point
│     ├─ app/
│     │  └─ gateway/
│     │     ├─ composition.py     # the only default Composition Root
│     │     ├─ bootstrap.py       # startup/close orchestration
│     │     └─ http/               # FastAPI Host and uvicorn adapter
│     ├─ platform/
│     │  └─ plugin_runtime/       # Catalog → Discovery → Loader → Manager
│     ├─ services/                 # public stateful runtime capabilities
│     │  ├─ config/ filesystem/ http/
│     │  ├─ messaging/{bus,channel}/
│     │  ├─ session/ agent/ tools/ workspace/
│     │  ├─ command/ attachment/ observability/
│     │  └─ system_prompt/
│     ├─ features/                 # optional product Plugins
│     │  ├─ skill/ mcp/ plan/ team/ schedule/
│     │  └─ context_govern/
│     └─ __init__.py                # package boundary
└─ tests/
   ├─ architecture/               # import boundaries and runtime contracts
   ├─ contracts/                   # Service contracts
   ├─ startup/ lifecycle/          # composition and reversible cleanup
   └─ hooks/                       # semantic Hook behavior and contracts
```

The retired root packages (`agent`, `api`, `bus`, `channel`, `plugin`, `session`,
etc.) are intentionally absent. New code belongs in `app`, `platform`,
`services` or `features`; external plugins use the explicit runtime manifest
boundary rather than importing private ftre modules.

## Startup and lifecycle

1. `ftre.main` parses CLI options and delegates to `app.gateway.bootstrap`.
2. `build_composition()` builds the default manifest list and creates a
   `PluginManager` over a public Cordis `Context`.
3. Required Service Plugins are activated first through declared dependencies;
   optional Feature and external Plugins are activated when enabled.
4. Each Plugin contributes Services, routes, hooks, tools or channels through
   the official `cordis.Context`; every cleanup is registered with a Cordis
   Effect factory such as `ctx.effect(lambda: disposer)`.
5. The real data plane binds the existing Session/Agent/Bus/Channel providers,
   freezes the HTTP registry, and starts the long-running Gateway.
6. Shutdown disposes Fibers in reverse order, then stops AgentLoop, Channels,
   schedulers and persistence. Cleanup is idempotent.

External modules are not imported during discovery. An entry is resolved only
after its manifest is explicitly enabled in `~/.ftre/config.json`:

```json
{
  "plugins": [
    {
      "id": "my-plugin",
      "entry": "my_plugin:apply",
      "enabled": true,
      "config": {}
    }
  ]
}
```

## Agent data plane

```text
Channel → MessageBus → AgentLoop → SessionLane → TurnExecutor
                                      ├─ agent/pre-step Hook → claim
                                      ├─ agent/after-turn Hook → next pending
                                      ├─ MailboxStore (pending only)
                                      └─ messages (durable chat history)

Context compaction is optional: when `ftre-compaction` is explicitly enabled,
its Service owns the pre-step/after-turn gates and overflow recovery. The core
SessionLane only provides the Hook barriers and generic maintenance state; it
does not import or construct a compaction implementation.
```

Different sessions run concurrently. A session has at most one active turn;
turn and compaction never overlap. Pending claims are at-most-once. These
invariants are covered by the SessionLane and lifecycle tests.

## Built-in capabilities

- **Services:** configuration, filesystem policy/IO, HTTP route registry,
  message bus, channels, sessions, agents/profiles, tools, workspaces,
  commands, attachments, traces and system prompts.
- **Features:** Skill catalog/loading, global/private MCP, Plan tool, Team
  orchestration, Schedule persistence and context governance hooks.
- **Extension boundary:** external plugins are loaded only through explicit
  `module:attribute` manifests; they do not import private ftre modules or
  define the core architecture.

## Development

```bash
# Official runtime, developed in the sibling E:\cordis-py repository.
python -m pip install -e E:\cordis-py
python -m pip install -e .[dev]
python -m pytest -q
python -m ruff check src tests
ftre gateway
```

The project does not contain a local `src/cordis` fallback. In CI or a clean
machine, install the reviewed `cordis-py` 0.4.0 release/source distribution
before installing ftre; verify with `python -c "import cordis; print(cordis.__file__)"`.

Configuration is read from `~/.ftre/config.json`; copy
`config.example.json` as a starting point. See `docs/PROCESS.md` for the PRD
workflow and `docs/COMMIT.md` for commit conventions.

## Related repositories

- [ftre-agent-core](https://github.com/quanming1/ftre-agent-core) — stateless Agent/LLM/Tool core
- [ftre-desktop](https://github.com/quanming1/ftre-desktop) — Electron + React client
- [ftre-docs](https://github.com/quanming1/ftre-docs) — documentation site

## License

[MIT](LICENSE)
