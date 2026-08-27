# ftre-agent

Stable Agent service contracts for the ftre platform.

This package is the contract-only half of the ftre Agent split (PRD-F33):

- `AgentService` — the public `agents` service entry (`run` / `cancel` / `status` / `is_busy` / `delete_session` / `resume_confirmation`), provided by `ftre_agent.plugin`
- `InboundMessage` — the single execution input, produced by the Inbox package after admission
- `AgentRunResult` — the single execution result (`completed` / `cancelled` / `failed`)
- `AgentRegistry` + `HookScopeCarrier` — agent identity and hook scope carriers
- Agent hooks (`agent/before-run`, `agent/after-run`, `agent/run-error`) plus re-exports of the Core-owned `agent/before-reasoning` / `agent/stop-decision` specs
- `AgentConfig` / `LLMConfig` — the frozen per-turn config snapshot shared by hooks, runtime and compaction

It deliberately contains **no** AgentLoop, LLM client, or tool execution. The
concrete runtime lives in [`ftre-agent-runtime`](../ftre-agent-runtime/), which
depends on this package and registers a Runtime Factory with the already-provided
`agents` service.

## Dependency boundary

`ftre-agent` never imports `ftre.services.*`. It can be installed standalone for
test doubles and alternative hosts. Disk config loading (`~/.ftre/config.json`)
stays in the ftre Host.
