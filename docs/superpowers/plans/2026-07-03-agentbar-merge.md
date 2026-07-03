# AgentBar: Merge Agent + Model Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the AgentSelector and ModelSelector into a single AgentBar capsule button. Clicking opens a panel showing agent info (name as header, workspace), with "switch agent" (header click) and "switch model" (model row + 切换 button) entries. Switching model writes to the agent's `agent.config.json` via new `PATCH /api/agents/{id}` endpoint.

**Architecture:** Backend adds `PATCH /api/agents/{agent_id}` to update agent.config.json llm field + cache invalidation. Frontend replaces two components with one `AgentBar.tsx` that embeds the existing `ModelPicker` component for model switching.

**Tech Stack:** Python 3.12 FastAPI, React TypeScript, Zustand, framer-motion

## Global Constraints

- Backend: `E:\ftre\src\ftre\`
- Frontend: `E:\binn\ftre-desktop\packages\renderer\src\`
- No new third-party dependencies
- `ModelPicker.tsx` and `providerInfo.ts` stay unchanged — reused by AgentBar
- `ModelSelector.tsx` and `AgentSelector.tsx` get deleted after AgentBar works
- Agent config files at `~/.ftre/agents/<id>/agent.config.json`

---

## Task 1: Backend — `update_agent()` in AgentManager + `PATCH /api/agents/{id}`

**Files:**
- Modify: `E:\ftre\src\ftre\agent\agent_manager.py` — add `update_agent()` method
- Modify: `E:\ftre\src\ftre\api\routes.py` — add `PATCH /api/agents/{agent_id}` route

**Interfaces:**
- Produces: `AgentManager.update_agent(agent_id: str, patch: dict) -> dict` — reads agent.config.json, merges patch into llm field, writes back, invalidates cache
- Produces: `PATCH /api/agents/{agent_id}` — body `{"llm": {"provider": "...", "model": "..."}}`

- [ ] **Step 1: Add `update_agent()` to AgentManager**

In `E:\ftre\src\ftre\agent\agent_manager.py`, add after `list_agents()` method (before `ensure_default`):

```python
    def update_agent(self, agent_id: str, patch: dict) -> dict:
        """更新 agent.config.json 的字段，目前只支持 llm。

        Args:
            agent_id: agent ID
            patch: {"llm": {"provider": "...", "model": "..."}}

        Returns:
            更新后的 agent.config.json 内容
        """
        agent_dir = self._agents_dir / agent_id
        if not agent_dir.is_dir():
            raise FileNotFoundError(f"agent '{agent_id}' 不存在")

        config_path = agent_dir / "agent.config.json"

        # 读取现有配置
        cfg: dict = {}
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[agent-manager] 读取 {config_path} 失败: {e}")

        # 合并 patch（目前只支持 llm 字段）
        if "llm" in patch and isinstance(patch["llm"], dict):
            existing_llm = cfg.get("llm", {})
            if not isinstance(existing_llm, dict):
                existing_llm = {}
            existing_llm.update(patch["llm"])
            cfg["llm"] = existing_llm

        # 写回
        config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 清除缓存
        self._cache.pop(agent_id, None)
        self._cache_key.pop(agent_id, None)

        logger.info(f"[agent-manager] 已更新 agent '{agent_id}' 的配置: {patch}")
        return cfg
```

- [ ] **Step 2: Add `PATCH /api/agents/{agent_id}` route**

In `E:\ftre\src\ftre\api\routes.py`, after the existing `GET /agents` route, add:

```python
@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, request: Request):
    """更新 agent.config.json 的字段。目前只支持 llm。"""
    if _agent_manager is None:
        raise HTTPException(status_code=503, detail="AgentManager 未初始化")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")

    if not isinstance(body, dict) or "llm" not in body:
        raise HTTPException(status_code=400, detail="目前只支持更新 llm 字段")

    try:
        updated = _agent_manager.update_agent(agent_id, body)
        return {"ok": True, "config": updated}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' 不存在")
    except Exception as e:
        logger.error(f"[api] 更新 agent 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 3: Verify compilation**

Run: `python -m py_compile src/ftre/agent/agent_manager.py && python -m py_compile src/ftre/api/routes.py`
Expected: no output

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_agent_manager.py -v --tb=short`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
cd E:\ftre
git add src/ftre/agent/agent_manager.py src/ftre/api/routes.py
git commit -m "feat: add PATCH /api/agents/{id} to update agent.config.json llm field"
```

---

## Task 2: Frontend — `updateAgent()` API function + store changes

**Files:**
- Modify: `E:\binn\ftre-desktop\packages\renderer\src\services\api.ts` — add `updateAgent()`
- Modify: `E:\binn\ftre-desktop\packages\renderer\src\stores\chat.ts` — model/provider derived from current agent

**Interfaces:**
- Produces: `updateAgent(agentId: string, patch: {llm: {provider, model}}) => Promise<boolean>`
- Produces: chat store `model` and `provider` now read from `agents` list based on current `agentId`

- [ ] **Step 1: Add `updateAgent()` to api.ts**

In `E:\binn\ftre-desktop\packages\renderer\src\services\api.ts`, after the `fetchChatAgents` function (after line 874), add:

```typescript
export async function updateAgent(
  agentId: string,
  patch: { llm: { provider: string; model: string } },
): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/api/agents/${encodeURIComponent(agentId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    return res.ok;
  } catch (e) {
    console.error("[api] updateAgent failed:", e);
    return false;
  }
}
```

- [ ] **Step 2: Add `updateAgentLlm` action to chat store**

In `E:\binn\ftre-desktop\packages\renderer\src\stores\chat.ts`, add to the store interface (after `fetchAgents`):

```typescript
  updateAgentLlm: (provider: string, model: string) => Promise<void>;
```

Add to the store implementation (after `fetchAgents` implementation):

```typescript
  updateAgentLlm: async (provider, model) => {
    const { agentId } = get();
    if (!agentId) return;
    const ok = await updateAgent(agentId, { llm: { provider, model } });
    if (ok) {
      set({ model, provider });
      // 刷新 agents 列表以更新缓存
      await get().fetchAgents();
    }
  },
```

Add `updateAgent` to the import at top of file:

```typescript
import { createSessionRemote, API_BASE, fetchChatAgents, updateAgent } from "@/services/api";
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd /d E:\binn\ftre-desktop && npx tsc --noEmit 2>&1 | findstr "api.ts chat.ts"`
Expected: no errors in target files

- [ ] **Step 4: Commit**

```bash
cd E:\binn\ftre-desktop
git add packages/renderer/src/services/api.ts packages/renderer/src/stores/chat.ts
git commit -m "feat: add updateAgent API + updateAgentLlm store action"
```

---

## Task 3: Frontend — Create AgentBar component

**Files:**
- Create: `E:\binn\ftre-desktop\packages\renderer\src\features\chat\AgentBar.tsx`

**Interfaces:**
- Consumes: `useChat` store (agentId, agents, model, provider, setAgentId, fetchAgents, updateAgentLlm), `ModelPicker` component, `buildProviderInfos`, `fetchAppConfig`, `getProviderLabel`
- Produces: `<AgentBar />` component replacing both `<AgentSelector />` and `<ModelSelector />`

- [ ] **Step 1: Create AgentBar.tsx**

```tsx
/**
 * AgentBar — 合并 Agent + Model 选择的单个胶囊按钮
 *
 * 点击展开面板：
 * - Header: Agent 名称（点击展开 Agent 列表切换）
 * - 工作区路径
 * - 当前模型 + [切换] 按钮（点击展开 ModelPicker）
 */
import { useState, useEffect, useRef, useCallback } from "react";
import { Check, ChevronDown, Settings2 } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { useChat } from "@/stores/chat";
import { useSession } from "@/stores/session";
import { fetchChatAgents, fetchAppConfig } from "@/services/api";
import { ModelPicker, type ProviderInfo } from "./ModelPicker";
import { buildProviderInfos, getProviderLabel } from "./providerInfo";
import { OPEN_SETTINGS_EVENT } from "@/app/settings-events";

export function AgentBar() {
  const agentId = useChat((s) => s.agentId);
  const agents = useChat((s) => s.agents);
  const model = useChat((s) => s.model);
  const provider = useChat((s) => s.provider);
  const setAgentId = useChat((s) => s.setAgentId);
  const setModel = useChat((s) => s.setModel);
  const setProvider = useChat((s) => s.setProvider);
  const setContextWindow = useChat((s) => s.setContextWindow);
  const fetchAgents = useChat((s) => s.fetchAgents);
  const updateAgentLlm = useChat((s) => s.updateAgentLlm);

  const sessionId = useChat((s) => s.sessionId);
  const sessions = useSession((s) => s.sessions);

  const [panelOpen, setPanelOpen] = useState(false);
  const [agentListOpen, setAgentListOpen] = useState(false);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const panelRef = useRef<HTMLDivElement>(null);

  const currentSession = sessions.find((s) => s.session_id === sessionId);
  const isScheduled = currentSession?.source === "scheduled";

  const current = agents.find((a) => a.id === agentId) || agents[0];
  const builtinAgents = agents.filter((a) => a.is_builtin);
  const customAgents = agents.filter((a) => !a.is_builtin);

  // 首次挂载拉取 agents
  useEffect(() => {
    if (agents.length === 0) {
      fetchAgents();
    }
  }, []);

  // 拉取 providers（用于 ModelPicker）
  const loadProviders = useCallback(async () => {
    const config = await fetchAppConfig();
    if (config && Object.keys(config).length > 0) {
      setProviders(buildProviderInfos(config.providers));
    }
  }, []);

  useEffect(() => {
    loadProviders();
  }, [loadProviders]);

  // 面板展开时刷新 agents
  useEffect(() => {
    if (panelOpen) {
      fetchAgents();
    }
  }, [panelOpen]);

  // 点击外部关闭
  useEffect(() => {
    if (!panelOpen) return;
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setPanelOpen(false);
        setAgentListOpen(false);
        setModelPickerOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [panelOpen]);

  // 模型显示名
  const modelDisplayName = (() => {
    if (!model) return "选择模型";
    for (const p of providers) {
      const m = p.models.find((mm) => mm.id === model);
      if (m) return m.name || m.id;
    }
    return model.length > 20 ? model.slice(0, 18) + "…" : model;
  })();

  // 切换模型
  const findContextWindow = (providerName: string, modelId: string): number | null => {
    const p = providers.find((x) => x.name === providerName);
    const m = p?.models.find((mm) => mm.id === modelId);
    return typeof m?.context_window === "number" ? m.context_window : null;
  };

  const handleSelectModel = async (providerName: string, modelId: string) => {
    setModel(modelId);
    setProvider(providerName);
    setContextWindow(findContextWindow(providerName, modelId));
    await updateAgentLlm(providerName, modelId);
    setModelPickerOpen(false);
  };

  const handleSelectAgent = (id: string) => {
    setAgentId(id);
    setAgentListOpen(false);
    // 切换 agent 后更新 model/provider
    const selected = agents.find((a) => a.id === id);
    if (selected?.model) {
      setModel(selected.model);
      setProvider(selected.provider || "");
      setContextWindow(findContextWindow(selected.provider || "", selected.model));
    }
  };

  if (isScheduled) {
    return (
      <div className="flex items-center gap-1.5 text-[13px] h-8 px-3 rounded-full font-mono text-t-dim cursor-default opacity-60">
        {current?.name || agentId}
      </div>
    );
  }

  const agentItemClass = (isActive: boolean) =>
    `w-full px-3 py-1.5 text-left text-[13px] font-mono flex items-center justify-between rounded-lg transition-all duration-150 ${
      isActive
        ? "text-[#1a1a1a] bg-[#e2e2e3]"
        : "text-t-secondary hover:text-t-primary hover:bg-hover"
    }`;

  return (
    <div className="relative" ref={panelRef}>
      {/* 胶囊按钮 */}
      <button
        onClick={() => setPanelOpen(!panelOpen)}
        className="flex items-center gap-1.5 text-[13px] h-8 px-3 rounded-full font-mono transition-colors duration-150 text-t-secondary hover:text-t-primary hover:bg-[#e7e7e8]"
      >
        <span className="truncate max-w-[100px]">{current?.name || agentId}</span>
        <span className="text-t-ghost">/</span>
        <span className="truncate max-w-[100px]">{modelDisplayName}</span>
        <ChevronDown size={12} className="shrink-0 opacity-60" />
      </button>

      <AnimatePresence>
        {panelOpen && (
          <motion.div
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="absolute bottom-full right-0 mb-1.5 w-[280px] bg-elevated border border-border-subtle rounded-xl overflow-hidden shadow-2xl z-[100]"
          >
            {/* Header: Agent 名称（点击展开 Agent 列表） */}
            <button
              onClick={() => { setAgentListOpen(!agentListOpen); setModelPickerOpen(false); }}
              className="w-full px-4 py-3 flex items-center justify-between hover:bg-hover transition-colors duration-150"
            >
              <div className="flex items-center gap-2">
                <span className="text-[14px] font-semibold text-t-primary">{current?.name || agentId}</span>
              </div>
              <ChevronDown
                size={14}
                className={`shrink-0 opacity-60 transition-transform duration-150 ${agentListOpen ? "rotate-180" : ""}`}
              />
            </button>

            <AnimatePresence>
              {agentListOpen && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.15, ease: "easeOut" }}
                  className="overflow-hidden"
                >
                  <div className="px-1.5 pb-1.5 max-h-[200px] overflow-y-auto">
                    {builtinAgents.map((agent) => {
                      const isActive = agentId === agent.id;
                      return (
                        <button
                          key={agent.id}
                          onClick={() => handleSelectAgent(agent.id)}
                          className={agentItemClass(isActive)}
                        >
                          <span className="truncate">{agent.name}</span>
                          {isActive && <Check size={14} className="shrink-0" />}
                        </button>
                      );
                    })}
                    {customAgents.length > 0 && (
                      <>
                        {builtinAgents.length > 0 && (
                          <div className="mx-1.5 my-1 border-t border-border-subtle" />
                        )}
                        <div className="px-2 pt-1.5 pb-1 text-[11px] text-t-ghost uppercase tracking-wider font-medium">
                          自定义
                        </div>
                        {customAgents.map((agent) => {
                          const isActive = agentId === agent.id;
                          return (
                            <button
                              key={agent.id}
                              onClick={() => handleSelectAgent(agent.id)}
                              className={agentItemClass(isActive)}
                            >
                              <span className="truncate">{agent.name}</span>
                              {isActive && <Check size={14} className="shrink-0" />}
                            </button>
                          );
                        })}
                      </>
                    )}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* 工作区 */}
            <div className="px-4 py-2 border-t border-border-subtle">
              <div className="text-[11px] text-t-ghost">工作区</div>
              <div className="text-[12px] text-t-secondary font-mono truncate mt-0.5">
                {current?.id === agentId ? (agents.find(a => a.id === agentId)) : current?.name}
              </div>
            </div>

            {/* 模型行 */}
            <div className="border-t border-border-subtle">
              <div className="px-4 py-2.5 flex items-center justify-between">
                <div className="min-w-0 flex-1">
                  <div className="text-[11px] text-t-ghost">模型</div>
                  <div className="text-[13px] text-t-primary font-mono truncate mt-0.5">
                    {modelDisplayName}
                  </div>
                </div>
                <button
                  onClick={() => { setModelPickerOpen(!modelPickerOpen); setAgentListOpen(false); }}
                  className="shrink-0 ml-3 text-[12px] px-2.5 py-1 rounded-md text-t-secondary hover:text-t-primary hover:bg-hover transition-colors duration-150"
                >
                  切换
                </button>
              </div>

              <AnimatePresence>
                {modelPickerOpen && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.15, ease: "easeOut" }}
                    className="overflow-hidden"
                  >
                    <div className="px-1.5 pb-1.5 max-h-[280px] overflow-y-auto">
                      {providers.map((p) => (
                        <div key={p.name}>
                          <div className="px-2 py-1 text-[11px] text-t-ghost uppercase tracking-wider font-medium">
                            {p.label}
                          </div>
                          {p.models.map((m) => {
                            const isActive = model === m.id && provider === p.name;
                            return (
                              <button
                                key={m.id}
                                onClick={() => handleSelectModel(p.name, m.id)}
                                className={agentItemClass(isActive)}
                              >
                                <span className="truncate">{m.name || m.id}</span>
                                {isActive && <Check size={14} className="shrink-0" />}
                              </button>
                            );
                          })}
                        </div>
                      ))}
                      {providers.length === 0 && (
                        <div className="px-3 py-2 text-[12px] text-t-ghost">加载中…</div>
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd /d E:\binn\ftre-desktop && npx tsc --noEmit 2>&1 | findstr "AgentBar"`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
cd E:\binn\ftre-desktop
git add packages/renderer/src/features/chat/AgentBar.tsx
git commit -m "feat: create AgentBar component merging agent + model selection"
```

---

## Task 4: Frontend — Wire AgentBar into ChatInput, remove old components

**Files:**
- Modify: `E:\binn\ftre-desktop\packages\renderer\src\features\chat\ChatInput.tsx`
- Delete: `E:\binn\ftre-desktop\packages\renderer\src\features\chat\AgentSelector.tsx`
- Delete: `E:\binn\ftre-desktop\packages\renderer\src\features\chat\ModelSelector.tsx`

- [ ] **Step 1: Replace AgentSelector + ModelSelector with AgentBar in ChatInput**

In `ChatInput.tsx`:

Replace the import of AgentSelector and ModelSelector:
```tsx
import { AgentBar } from "./AgentBar";
```
Remove:
```tsx
import { AgentSelector } from "./AgentSelector";
import { ModelSelector } from "./ModelSelector";
```

Replace the JSX (around line 862-864):
```tsx
            {/* 右侧：Agent/模型 + 上下文用量 + 发送 */}
            <div className="flex items-center gap-1.5">
              <AgentBar />
              <div className="w-px h-3.5 bg-border-subtle mx-1" />
```

Remove the `handleModelChanged` callback (lines 339-343) — no longer needed.

- [ ] **Step 2: Delete old components**

```bash
cd /d E:\binn\ftre-desktop
del packages\renderer\src\features\chat\AgentSelector.tsx
del packages\renderer\src\features\chat\ModelSelector.tsx
```

- [ ] **Step 3: Clean up any remaining imports of deleted components**

Search for references:
```
powershell -c "Get-ChildItem -Recurse packages\renderer\src -Include *.ts,*.tsx | Select-String -Pattern 'AgentSelector|ModelSelector' | Select-Object Path, LineNumber"
```

Fix any remaining imports (ModelSelector may be used elsewhere — if so, keep the file; if only used in ChatInput, deletion is safe).

- [ ] **Step 4: Verify TypeScript compiles**

Run: `cd /d E:\binn\ftre-desktop && npx tsc --noEmit 2>&1 | findstr "ChatInput\|AgentBar\|AgentSelector\|ModelSelector"`
Expected: no errors (or only pre-existing errors in other files)

- [ ] **Step 5: Commit**

```bash
cd E:\binn\ftre-desktop
git add packages/renderer/src/features/chat/ChatInput.tsx
git rm packages/renderer/src/features/chat/AgentSelector.tsx
git rm packages/renderer/src/features/chat/ModelSelector.tsx
git commit -m "feat: replace AgentSelector + ModelSelector with AgentBar in ChatInput"
```

---

## Self-Review

### 1. Spec Coverage

| Spec Requirement | Task |
|-----------------|------|
| Merge Agent + Model into one capsule button | Task 3 |
| Click expands panel with agent info | Task 3 |
| Header = agent name, click to switch agent | Task 3 |
| Model row with current model + 切换 button | Task 3 |
| 切换模型 writes to agent.config.json via PATCH | Task 1 + Task 2 |
| Backend PATCH /api/agents/{id} | Task 1 |
| Frontend updateAgent API | Task 2 |
| Remove old AgentSelector + ModelSelector | Task 4 |

### 2. Placeholder Scan

No placeholders — all code blocks contain complete implementations.

### 3. Type Consistency

- `updateAgent(agentId: string, patch: {llm: {provider, model}})` — consistent across api.ts and chat.ts
- `updateAgentLlm(provider, model)` — defined in store interface, used in AgentBar
- `AgentBar` reads `model`, `provider`, `agentId`, `agents` from store — all exist
- `ModelPicker` component NOT used directly in AgentBar — model list is rendered inline to match the collapsible panel UX. ModelPicker.tsx is preserved for potential reuse elsewhere.
