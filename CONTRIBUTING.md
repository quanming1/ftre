# 贡献指南

感谢你对 FTRE 的兴趣！本文档描述如何参与开发。

## 开发环境

```bash
# Clone 所有四个仓库到同级目录
git clone https://github.com/quanming1/ftre.git
git clone https://github.com/quanming1/ftre-agent-core.git
git clone https://github.com/quanming1/ftre-desktop.git
git clone https://github.com/quanming1/ftre-docs.git

# 安装后端依赖
cd ftre-agent-core && pip install -e . && cd ..
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

### Python（ftre + agent-core）

- Python 3.12+，使用类型注解
- 日志统一用 `logging`（Python）
- 测试用 `pytest` + `pytest-asyncio`

### TypeScript（desktop）

- 严格模式
- 日志统一用 `console`（前端）

## 测试

提交前确保测试通过：

```bash
# agent-core
cd ftre-agent-core
python -m pytest tests/ -q --ignore=tests/test_fake_llm.py --ignore=tests/test_probe_real_model.py

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

改 `ftre-agent-core` 后需要同步验证 `ftre` 后端（`ftre` import `ftre-agent-core`）。改前端后需要同步验证后端 API 兼容性。
