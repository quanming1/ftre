# EXECUTION-F39 ConfigService 外部变更热更新与模型目录

## 执行信息

| 字段 | 值 |
|---|---|
| 阶段 | F39 |
| 日期 | 2026-08-30 |
| 分支 | `feature/F39-config-runtime-reload` |
| PRD | `docs/prd/PRD-F39-config-runtime-reload-and-model-catalog.md` |
| 状态 | 已验收 |

## 实现结果

- `ConfigService` 增加 `source_path/content_hash` 快照字段、外部文件指纹检测、
  `reload_if_changed/reload`、安全错误保留和可逆 watcher。
- `Config Plugin` 负责 watcher 启停；`GET /api/config/models` 返回带 revision 的脱敏
  Provider/Model 目录。
- Agent Profile、Compaction 的生产配置读取通过注入的 ConfigService；独立单元测试仍可
  显式使用 loader，不形成 Gateway 运行时第二 Owner。
- Desktop `AgentBar` 在模型面板打开时刷新脱敏目录；请求失败保留已有列表，兼容旧配置接口。
- Core 仓库未参与实现；`E:\ftre-agent-core` 已废弃并清除内容。

## 验证记录

| 检查项 | 结果 |
|---|---|
| F39 配置/生命周期专项 | 通过，17 tests |
| 后端全量 `python -m pytest -q` | 通过，766 passed |
| 后端 `python -m ruff check src packages tests --no-cache` | 通过 |
| 后端 `git diff --check` | 通过 |
| 客户端 renderer 全量测试 | 通过，61 files / 570 tests |
| 客户端 renderer TypeScript | 通过 |
| 客户端 renderer production build | 通过；仅保留既有 CSS/dynamic import/chunk warnings |

## 手动等价验收

1. Gateway 已启动时修改 `providers.<name>.models`，随后请求 `/api/config/models`，新模型
   出现在返回目录，revision 只递增一次。
2. 将配置暂时写成非法 JSON，接口继续返回上一次有效目录；恢复合法 JSON 后自动加载。
3. 打开客户端模型面板，调用最新模型目录；当前模型存在时保持选择，网络失败不清空列表。
4. 关闭 Composition 后 watcher 与异步通知任务均完成清理。

## 交付状态

本阶段代码、测试、PRD、TODO 和 CHANGELOG 已完成；本轮未执行 commit、push 或 PR 合入。
