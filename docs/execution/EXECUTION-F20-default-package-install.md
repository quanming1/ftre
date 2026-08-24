# F20 执行报告：默认安装与装配全部仓内 Package

## 结果

F20 已完成。`ftre` 默认发行组合现在包含 `packages/` 下五个独立 Package：

```text
ftre-inbox       → inbox
ftre-compaction  → compaction
ftre-messaging   → messaging
ftre-task        → task
ftre-team        → team
```

Package 仍保持独立 entry point、Service/Hook/Tool Owner 和可逆生命周期；默认安装只是把
它们纳入 Host 的发行组合，不合并源码或职责。业务 Package 仍可以通过 `enabled: false`
禁用，Inbox 继续是当前 Gateway 的必选数据面。

## 实施内容

- 根 `pyproject.toml` 的默认 dependencies 增加五个 Package；原有单能力 extras 和 `full`
  保留为裁剪安装兼容入口。
- Composition 增加 `compaction` Manifest；Inbox、Compaction、Messaging、Task、Team
  默认装配，缺失时由 Plugin diagnostics 记录，业务 Package 不成为 Agent Runtime 的硬编码依赖。
- 新增 F20 架构门禁、默认生命周期和禁用回归测试。
- 用户配置 `C:\Users\蒋全明\.ftre\config.json` 已启用：

  ```json
  {"id": "compaction", "enabled": true}
  ```

## 验证记录

| 命令/检查 | 结果 |
|---|---|
| F20/Compaction/Package/生命周期/启动专项 | 20 passed |
| `python -B -m pytest -q` | 509 passed in 135.77s |
| `python -B -m ruff check src tests packages --no-cache` | passed |
| `git diff --check` | passed |
| 生成物扫描（`__pycache__`、`.pytest_cache`、`.ruff_cache`、build/dist/egg-info） | 0 |
| `PIP_NO_INDEX=1 python -m pip install --no-build-isolation -e E:\\ftre` | passed；五个本地 Package 均已安装并满足根依赖 |
| 默认 Composition 状态 | `inbox/compaction/messaging/task/team` 全部 `ACTIVE` |
| Gateway smoke + `GET http://127.0.0.1:48650/api/commands` | 200；返回 `/compress-fast`、`/compact`，source=`ftre-compaction` |

## 客户端使用

客户端启动时通过既有 `/api/commands` 动态读取命令；刷新客户端后可见：

- `/compress-fast`：无 LLM 的旧工具输出快速裁剪；
- `/compact`：调用摘要模型进行上下文压缩。

`/compress` 不是当前协议名称，本阶段没有新增该别名。

## 工程卫生

本阶段未修改客户端、`E:\\ftre-agent-core` 或 `E:\\cordis-py`，未执行 commit、push、merge 或
release。当前本地 Gateway 已重新启动于 `48650`，并以 `PYTHONDONTWRITEBYTECODE=1` 运行，供客户端使用。
仓库原有未提交改动保持不动。PyPI 发布仍是独立发行任务；在包尚未发布前，洁净环境需从本地
`packages/` editable/wheel 安装五个 Package 后再安装 Host。
