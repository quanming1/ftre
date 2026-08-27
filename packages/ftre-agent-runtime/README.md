# ftre-agent-runtime

The concrete Agent runtime implementation for the ftre platform (PRD-F33).

Contents:

- `plugin.py` — the single Runtime provider plugin (entry point `agent-runtime`), registering the private runtime as the `agents` service's Runtime Factory
- `engine.py` — `AgentLoop`: active-turn runtime, agent hook dispatch, cancellation, maintenance barrier
- `turn_executor.py` — the Turn state machine (`BUILDING → RUNNING → FINALIZING → terminal`)
- `factory.py` — the only Core `ReActAgent` construction point
- `state.py` — private Turn state (public result type is `ftre_agent.AgentRunResult`)
- `completion.py` — in-process completion registry for `request_id` waiters

## Dependency direction

```
ftre-agent-runtime → ftre-agent (contracts) → ftre-agent-core
                  → ftre-llm (service adapter)
```

Host services (sessions, tools, message_bus, system_prompt, agent_profiles, llm,
hook_runtime, session_events, config, workspaces) are injected through the
provider plugin and consumed via their narrow public methods only. This package
never imports `ftre.services.*` implementation modules, so it can be installed
and exercised in a clean environment without the ftre Host.

## Loading

```
[project.entry-points."ftre.plugins"]
agent-runtime = "ftre_agent_runtime.plugin:apply"
```

The Host Composition Root loads this entry point; it never constructs
`AgentLoop` by hand.
