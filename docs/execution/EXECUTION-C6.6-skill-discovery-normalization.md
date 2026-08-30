# EXECUTION-C6.6 Skill 发现规则与作用域稳定性修复

## 执行信息

| 字段 | 值 |
|---|---|
| 阶段 | C6.6 |
| 日期 | 2026-08-30 |
| 分支 | `feature/C6-skill-discovery`（保留原有 F39 未提交改动） |
| PRD | `docs/prd/PRD-C6-generic-markdown-extension-protocol.md` |
| 状态 | 已完成实现，待按仓库流程提交 |

## 实现结果

- SkillService 删除按 README/LICENSE 等文件名判断资格的黑名单；根目录 `.md` 统一作为候选，只有合法 YAML frontmatter 才进入目录。
- frontmatter `name` 成为唯一规范名称；目录名和文件 basename 只负责定位，名称不一致不再拒绝；global CRUD 按 YAML name 查找真实文件。
- Skill 列表补充 `origin/source`，客户端正确区分 global、Agent、workspace 和 `r0/r1` 外部来源；外部/项目/Agent Skill 在管理面板只读。
- 输入框和 Skill 管理面板按当前 Session 的 `agent_id/workspace` 查询，作用域变化取消旧请求；管理面板可展开查看扫描诊断。
- 修复用户 Skill 文件：`prompt-engineering` YAML 引号、`first-principles` name、`firecrawl-lean` allowed-tools、`using-llm-wiki` 编码和默认 Agent 的嵌套 metadata；`meituan-travel` 按 YAML name 识别。

## 验证记录

| 检查项 | 结果 |
|---|---|
| 后端 Skill 契约专项 | 通过，17 tests |
| 后端全量 `pytest -q` | 通过，767 passed |
| 后端 `ruff check src packages tests` | 通过 |
| 后端扫描实际用户 Skill | 19 个有效 Skill，0 个 diagnostics；包含 `prompt-engineering`、`meituan-travel` |
| 客户端 Skill/Chat 专项 | 通过，21 tests |
| 客户端 renderer 全量 | 通过，61 files / 572 tests |
| 客户端 TypeScript | 通过 |
| 客户端 production build | 通过；仅有既有 CSS、动态 import 和 chunk 大小警告 |
| 两仓库 `git diff --check` | 通过 |

## 手工等价验收

1. `~/.ftre/skills/prompt-engineering/SKILL.md` 在 `/api/skills?agent_id=default` 中可见。
2. `~/.ftre/agents/default/skills/meituan-trip/SKILL.md` 以 YAML `name: meituan-travel` 出现在目录中。
3. Codex `r0` 与 Agents `r1` root 继续被扫描，外部来源不会被客户端映射成 global。
4. 根目录无 YAML 的 README 只出现在 diagnostics，不出现在 Skill 列表。
5. Session 切换到其它 Agent 后，输入框和详情请求使用该 Session 的 Agent 作用域。

## 交付状态

C6.6 代码、测试、PRD、TODO 和执行记录已完成；工作区原有 F39 变更未被覆盖，当前未执行 commit、push 或 PR 合入。
