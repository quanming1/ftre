# PRD-F39 ConfigService 外部变更热更新与模型目录

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F39 |
| 名称 | ConfigService 外部变更热更新与模型目录 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-30 |
| 定稿日期 | 2026-08-30 |
| 验收日期 | 2026-08-30 |
| 关联文档 | `docs/TODO.yaml`、`AGENTS.md`、PRD-F1、PRD-F29 |

## 1. 背景与目标

当前 Gateway 启动时由 `ConfigService` 读取一次 `~/.ftre/config.json` 并持有内存快照。
用户通过编辑器直接修改文件后，外部变更不会进入该快照，`GET /api/config` 继续返回旧的
providers/models，桌面端模型下拉因此看不到新模型。与此同时，Agent Profile 仍存在独立
读取和缓存根配置的路径，导致不同消费者可能看到不同版本。

本阶段目标：

> 让配置文件的外部合法变更在运行中的 Gateway 内被 ConfigService 发现、校验、发布，
> 并让客户端从脱敏的模型目录读取最新 Provider/Model；无效编辑不得污染当前有效配置。

非目标：

- 不修改 Agent ReAct、LLM 请求协议、Retry/Fallback、Inbox、Session wire 或 Cordis Kernel。
- 不改变现有 `config.json` 字段语义，不引入远程配置中心或额外数据库。
- 不在本阶段重做模型设置页的密钥编辑交互；现有兼容的 `/api/config` 写入保持可用。
- 不恢复或依赖已退休的 `ftre-agent-core`。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：快照包含来源和内容指纹。** `ConfigSnapshot` 除 `revision/value` 外提供
  `source_path` 和 `content_hash`；revision 在进程内单调递增，初始读取为 0。
- [x] **FR2：发现外部文件变化。** ConfigService 必须能检测 `config.json` 的原子替换、
  mtime 变化或内容 hash 变化；自 Service 写盘产生的同一内容不得重复 reload/通知。
- [x] **FR3：安全 reload。** `reload()`/`reload_if_changed()` 读取并校验完整 JSON 对象；
  合法且 hash 变化时替换 active snapshot、递增 revision 并通知 watcher；JSON 无效、根值
  非对象或 IO 失败时保留旧 snapshot，仅记录结构化错误日志。
- [x] **FR4：生命周期收口。** Config Plugin 启动一个可取消的文件监听任务，监听任务、定时器
  和 watcher 在 Context dispose 时全部停止且幂等；监听不能阻塞 HTTP/Agent 主循环。
- [x] **FR5：统一模型目录。** ConfigService 提供从当前快照生成的脱敏 ModelCatalog：
  provider、model id/name、api_type、context_window、max_output、vision、
  reasoning_effort_values；不得包含 `api_key` 或其他凭据。
- [x] **FR6：模型目录 HTTP 接口。** 新增 `GET /api/config/models`，返回 catalog 与 revision；
  原 `GET /api/config` 和 `PUT /api/config` 保持兼容，并使用最新快照。
- [x] **FR7：重复根配置读取收口。** Agent Profile 的运行时根配置读取优先使用注入的
  ConfigService 快照；删除或隔离 `load_config_file()`/模块级缓存对同一根配置的第二事实源。
  Agent 私有 `agent.config.json` 仍由 AgentProfile Owner 管理。
- [x] **FR8：客户端刷新。** 模型选择器打开时请求最新模型目录；收到 revision 变化或请求
  失败时显示可恢复状态，不清空当前可用模型。设置页仍可通过兼容接口保存配置。

### 2.2 非功能需求

- **性能**：监听采用轻量级 debounce/polling，单次文件检测不阻塞请求；模型目录生成不做
  LLM 或网络调用。
- **一致性**：同一 `content_hash` 只产生一次 revision 和一次 watcher 通知；合法外部编辑
  在下一次快照/模型目录请求或监听周期内可见。
- **安全性**：模型目录绝不返回凭据；日志只记录路径、revision、hash 摘要和错误类型。
- **兼容性**：Python 3.12；保留现有 `/api/config` 请求形状和配置字段；不改变 Agent Profile
  私有文件格式。

## 3. 技术方案

### 3.1 模块边界

```text
ConfigStore
  └─ 负责 JSON 文件读写、原子写和内容指纹

ConfigService
  ├─ active ConfigSnapshot
  ├─ reload / reload_if_changed
  ├─ revision/hash 去重
  └─ watcher 分发

Config Plugin
  └─ 负责监听任务的启动、取消和 Effect 清理

Config Router
  ├─ /api/config        兼容配置读写
  └─ /api/config/models 脱敏模型目录

AgentProfile / LLM
  └─ 只消费 ConfigService，不再直接读取同一根 config.json
```

目标文件：

- `src/ftre/services/config/service.py`
- `src/ftre/services/config/store.py`
- `src/ftre/services/config/plugin.py`
- `src/ftre/services/config/router.py`
- `src/ftre/services/config/loader.py`（收口或标记为启动期唯一适配）
- `src/ftre/services/agent_profile/config.py`
- `src/ftre/services/agent_profile/manager.py`
- `packages/ftre-llm/src/ftre_llm/contracts.py`（仅在模型目录契约需要复用时）
- `E:\binn\ftre-desktop\packages\renderer\src\services\api.ts`
- `E:\binn\ftre-desktop\packages\renderer\src\features\chat\AgentBar.tsx`

### 3.2 数据模型

```python
@dataclass(frozen=True)
class ConfigSnapshot:
    revision: int
    value: dict[str, Any]
    source_path: str
    content_hash: str

@dataclass(frozen=True)
class ModelCatalog:
    revision: int
    providers: tuple[ProviderCatalog, ...]

@dataclass(frozen=True)
class ProviderCatalog:
    name: str
    api_type: str
    models: tuple[ModelCatalogItem, ...]
```

`ModelCatalogItem` 只包含客户端展示和能力选择所需字段，不包含密钥、完整 provider
配置或内部路径。

### 3.3 读取和通知流程

```text
外部编辑器原子替换 config.json
        ↓
Config watcher / snapshot lazy check 发现 hash 变化
        ↓
JsonConfigStore 读取并校验
        ├─ 无效：保留旧 snapshot + 记录 CONFIG_INVALID
        └─ 有效：revision + 1，替换 snapshot
                    ↓
             watcher 只通知一次
                    ↓
         /api/config/models 返回新 revision
                    ↓
             客户端刷新 ModelPicker
```

监听实现可以使用 asyncio 任务与可配置间隔，禁止新增常驻进程和阻塞式无限循环。
`snapshot()` 或模型目录请求必须在必要时执行一次轻量 `reload_if_changed()`，避免监听
周期尚未到达时客户端仍读到旧值。

### 3.4 错误和并发边界

- Service 自己的 `update/replace` 继续使用原子写和 expected revision。
- 外部修改不带 expected revision；以文件 hash 去重，以 Service 当前快照为 active 版本。
- 外部文件处于半写入状态时不覆盖 active snapshot；下一次检测继续尝试。
- watcher 回调失败只记录错误，不回滚已发布的有效 snapshot。
- reload 不读取 Agent Profile 私有文件；私有文件由 AgentProfileService 单独管理。

## 4. 接口定义

### 4.1 ConfigService

```python
def snapshot(self) -> ConfigSnapshot
def reload_if_changed(self) -> ConfigSnapshot
async def reload(self, *, force: bool = False) -> ConfigSnapshot
def watch(self, callback) -> Callable[[], bool]
def model_catalog(self) -> ModelCatalog
```

### 4.2 HTTP

```text
GET /api/config
→ 兼容现有配置对象，但来源为最新 active snapshot

GET /api/config/models
→ {
     "revision": 3,
     "providers": [
       {
         "name": "OpenCode 直连",
         "api_type": "completions",
         "models": [{"id": "...", "name": "...", "vision": true}]
       }
     ]
   }

PUT /api/config
→ 保持现有 replace/If-Config-Revision 兼容语义
```

### 4.3 客户端

`api.ts` 增加 `fetchModelCatalog()`；`AgentBar` 在模型面板打开时刷新，并按 revision
替换 Provider 列表。当前选择仍存在时保持选择；当前选择被删除时显示明确的不可用状态，
不静默切换到其他模型。

## 5. 验收标准

- [x] **AC1：** Gateway 启动后直接在磁盘新增一个 model，下一次 `GET /api/config/models`
  能返回该 model，无需重启 Gateway。
- [x] **AC2：** 外部合法修改使 revision 恰好递增一次，重复 polling 不重复通知。
- [x] **AC3：** 外部写入非法 JSON 时 API 仍返回上一个有效 catalog，并记录 `CONFIG_INVALID`；
  修复文件后自动恢复。
- [x] **AC4：** `GET /api/config/models` 响应不含 `api_key`、`secret` 或完整凭据字段。
- [x] **AC5：** Config Plugin dispose 后无 watcher task、定时器或未移除 callback。
- [x] **AC6：** Agent Profile、LLM resolve 和模型目录在同一 revision 下看到相同 providers/models；
  运行时不再通过第二缓存读取根配置。
- [x] **AC7：** 客户端打开模型面板可看到刚刚写入的新模型；网络失败不清空已有列表。
- [x] **AC8：** 现有配置保存、Agent 执行、Compaction、标题生成、Gateway 启停回归通过。
- [x] **AC9：** `pytest`、`ruff check src packages tests`、`git diff --check` 和客户端
  TypeScript/测试通过。

## 6. 测试计划

- ConfigService 临时文件：初始快照、mtime/hash 变化、原子替换、重复通知、非法 JSON、
  根值类型错误、IO 失败、dispose。
- Router：模型目录字段白名单、revision、凭据脱敏、兼容 `/api/config`。
- AgentProfile/LLM：注入 ConfigService 后不再读取第二份根配置，外部变更后一致解析。
- Desktop：打开 ModelPicker 触发刷新、新模型可见、当前选择保留、请求失败保持旧列表。
- Lifecycle：启动/卸载 Config Plugin 后 asyncio task 和 watcher 数量归零。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-30 | 新建 F39，定义 ConfigService 外部变更热更新、脱敏模型目录和客户端刷新边界 | 修复直接编辑 `~/.ftre/config.json` 后模型下拉看不到新模型的问题；同时收口已发现的重复配置读取 |
| 2026-08-30 | 完成 F39.1-F39.3 首轮实现：文件指纹/reload、Config Plugin watcher、脱敏 `/api/config/models`、Agent Profile/Compaction 生产路径改用 ConfigService | 确保外部配置变更进入统一快照，并避免根配置出现第二个运行时 Owner；进入跨仓测试阶段 |
| 2026-08-30 | F39 验收完成：后端 766 passed、Ruff 全绿；客户端 570 passed、TypeScript 与生产构建通过；PRD/TODO/CHANGELOG 已同步 | 所有 FR/AC 已按测试和手动等价场景核对通过 |
