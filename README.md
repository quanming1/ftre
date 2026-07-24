# FTRE

English | [中文](README.zh-CN.md)

FTRE is a local-first AI coding assistant, consisting of four interconnected projects:

| Project | Repo | Role |
|---|---|---|
| ftre-agent-core | [quanming1/ftre-agent-core](https://github.com/quanming1/ftre-agent-core) | ReAct, LLM, Tool & tracing core (stateless algorithm library) |
| ftre | [quanming1/ftre](https://github.com/quanming1/ftre) | Gateway backend: Session, Channel, MCP, plugins, HTTP API |
| ftre-desktop | [quanming1/ftre-desktop](https://github.com/quanming1/ftre-desktop) | Electron + React desktop client |
| ftre-docs | [quanming1/ftre-docs](https://github.com/quanming1/ftre-docs) | Documentation site (React + Vite) |

```
ftre-agent-core    Agent core library (stateless, pure algorithm)
      │              ReActAgent / LLMHandler / Tool system / Runner
      │              Imported by ftre backend, not deployed standalone
      │
      ▼
ftre               Gateway backend (stateful, long-running process)
      │              Session management / EventBus / Channel / Plugins / MCP
      │              Built-in plugins: skill, mcp, context_govern, title_gen
      │              Provides WebSocket + HTTP API to desktop
      ▼
ftre-desktop        Desktop client (Electron + React)
                     GUI: chat interface, editor, Inspector panel, settings
                     Communicates with backend via WebSocket
      ▼
ftre-docs          Documentation site (independent deployment)
```

## Quick Start

### Prerequisites

- Python 3.12+
- Node.js 18+ / pnpm
- An OpenAI-compatible LLM API key

### 1. Clone repositories

```bash
git clone https://github.com/quanming1/ftre.git
git clone https://github.com/quanming1/ftre-agent-core.git
git clone https://github.com/quanming1/ftre-desktop.git
git clone https://github.com/quanming1/ftre-docs.git
```

Place all four repos in the same parent directory:

```
parent/
├── ftre/
├── ftre-agent-core/
├── ftre-desktop/
└── ftre-docs/
```

### 2. Install dependencies

```bash
# Backend + agent-core
cd ftre-agent-core
pip install -e .
cd ../ftre
pip install -e .

# Frontend
cd ../ftre-desktop
pnpm install

# Docs site
cd ../ftre-docs
pnpm install
```

### 3. Configure

Copy the example config and add your API key:

```bash
mkdir -p ~/.ftre
cp ftre/config.example.json ~/.ftre/config.json
# Edit ~/.ftre/config.json and fill in your api_key
```

### 4. Run

Two terminals:

```bash
# Terminal 1 — Backend
ftre gateway

# Terminal 2 — Desktop client (ftre-desktop repo)
cd ftre-desktop && pnpm dev
```

## Project Structure

```
ftre/
├── src/ftre/
│   ├── agent/          # AgentLoop — consumes inbound messages, drives Agent execution
│   ├── bus/            # EventBus — in-process message bus
│   ├── channel/        # Channel — WebSocket / SubAgent communication channels
│   ├── command/        # Command system (/compact, /cancel, etc.)
│   ├── config.py       # Loads config from ~/.ftre/config.json
│   ├── main.py         # Entry point: FastAPI Gateway service
│   ├── plugin/         # Built-in plugins (skill, mcp, context_govern, title_gen)
│   ├── session/        # SessionManager — SQLite persistence
│   ├── tools/          # 8 built-in tools
│   └── trace_store.py  # Agent Tracing SQLite exporter
├── tests/
├── config.example.json # Example configuration
└── pyproject.toml
```

## Core Features

### Built-in Tools

8 built-in tools (`src/ftre/tools/`): bash, read, write, edit, set_workspace, cron, task, send_message.

- **read/write/edit** return `(result_str, diff_metadata)` tuples; the desktop Inspector panel displays diff previews and file snapshots
- **bash** supports RTK auto-rewriting (reduces token usage) and semble semantic code search integration
- **task** sub-agent mode dispatches tasks to independent sessions for synchronous execution
- Tools are filtered per Agent config (`tools.allow` / `tools.deny`)

### Multi-Agent Architecture

Each Agent has an independent config directory `~/.ftre/agents/<agent_id>/`, supporting independent LLM, tools, MCP, Skills, and workspace configuration.

### MCP Dual-Layer Configuration

| Layer | Config source | Registration target |
|------|----------|----------|
| Public MCP | `config.json` `mcp` section | Global `tool_registry` (shared by all Agents) |
| Private MCP | `agent.config.json` `mcp` section | Per-agent `tool_registry` |

### Inspector Panel

Desktop right-side extension panel with:
- **File preview**: Monaco editor read-only rendering, content snapshot from read tool metadata
- **Diff preview**: edit/write tools open side-by-side diff view
- **File tree sidebar**: workspace directory browsing, git status markers (negotiated cache polling), image preview
- **Changes node**: flat list of all git-changed files with line counts and status markers

### Hook System

Fully async filter chain with two hook points:
- `before_messages_build`: event stream governance + AGENTS.md injection
- `before_agent_run`: MCP/Skill system prompt injection + private tool registration

### Plugin System

4 built-in plugins (shipped with code): `skill`, `mcp`, `context_govern`, `title_gen`. External plugins directory: `~/.ftre/plugins/`.

### Agent Tracing

The Gateway automatically records a tree-shaped Trace for each Agent execution. The Desktop left-side **Traces** panel shows Trace list, Run tree, and full details.

Read-only API:
- `GET /api/traces?limit=100`: recent Trace summaries
- `GET /api/traces/{trace_id}`: single Trace's Run tree
- `GET /api/traces/{trace_id}/runs/{run_id}`: single Run's full payload

> Traces contain full prompts and tool inputs/outputs. Review for sensitive information before sharing or archiving JSONL files.

## Configuration

Global config: `~/.ftre/config.json` (see `config.example.json`)

Agent config: `~/.ftre/agents/<agent_id>/agent.config.json`

Full documentation: [ftre-docs](https://github.com/quanming1/ftre-docs)

## Tech Stack

- **Backend**: Python 3.12 + asyncio + FastAPI
- **Frontend**: TypeScript + React + Electron + Vite
- **Editor**: Monaco Editor
- **LLM**: OpenAI-compatible API (via ftre-agent-core's LLMHandler)
- **Storage**: SQLite

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
