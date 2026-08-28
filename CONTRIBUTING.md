# 贡献指南

感谢你对 FTRE 的兴趣！本文档描述如何参与开发。

## 开发环境

```bash
# Clone 需要参与开发的仓库
git clone https://github.com/quanming1/ftre.git
git clone https://github.com/quanming1/ftre-desktop.git
git clone https://github.com/quanming1/ftre-docs.git

# 安装后端依赖
cd ftre && pip install -e . && cd ..

# 安装前端依赖
cd ftre-desktop && pnpm install && cd ..
```

## 分支规范

- `master` / `main`：稳定分支，不接受直接 push
- `feat/<name>`：新功能
- `fix/<name>`：bug 修复
- `refactor/<name>`：重构

## Commit 规范

```
<type>: <description>

[optional body]
```

type 包括：`feat`、`fix`、`refactor`、`docs`、`test`、`chore`。

示例：`feat: add MCP server hot-reload support`

## 代码风格

### Python（ftre 与 packages）

- Python 3.12+，使用类型注解
- 日志统一用 `logging`（Python）
- 测试用 `pytest` + `pytest-asyncio`

### TypeScript（desktop）

- 严格模式
- 日志统一用 `console`（前端）

## 测试

提交前确保测试通过：

```bash
# ftre
cd ftre
python -m pytest tests/ -q
```

## PR 流程

1. 从 `master` / `main` 切出功能分支
2. 编写代码 + 测试
3. 确保 CI 通过
4. 提交 PR，描述改动内容和动机

## 仓库关系

Agent 契约、Runtime 和 LLM 的改动均在 `ftre/packages/` 内完成；改前端后需要同步验证后端 API 兼容性。
