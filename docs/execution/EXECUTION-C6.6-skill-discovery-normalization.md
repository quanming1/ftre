# EXECUTION-C6.6 Skill 发现规则与作用域稳定性修复

## 执行信息

| 字段 | 值 |
|---|---|
| 阶段 | C6.6 |
| 日期 | 2026-08-30 |
| 分支 | `feature/C6-skill-discovery`（保留原有 F39 未提交改动） |
| PRD | `docs/prd/PRD-C6-generic-markdown-extension-protocol.md` |
| 状态 | 基础实现已提交；本轮审计补丁已完成专项验证 |

## 实现结果

- SkillService 删除按 README/LICENSE 等文件名判断资格的黑名单；根目录 `.md` 统一作为候选，只有合法 YAML frontmatter 才进入目录。
- frontmatter `name` 成为唯一规范名称；目录名和文件 basename 只负责定位，名称不一致不再拒绝；global CRUD 按 YAML name 查找真实文件。
- Skill 列表补充 `origin/source`，客户端正确区分 global、Agent、workspace 和 `r0/r1` 外部来源；外部/项目/Agent Skill 在管理面板只读。
- 输入框和 Skill 管理面板按当前 Session 的 `agent_id/workspace` 查询，作用域变化取消旧请求；管理面板可展开查看扫描诊断。
- 模型提示的扫描规则与发现实现保持一致：README 只有带合法 frontmatter 才可能成为平铺 Skill，根目录 `SKILL.md` 仍为特殊文件；`loadSkill` 工具从当前运行上下文注入 Agent/工作区，避免私有 Skill 回退到默认作用域。
- 修复用户 Skill 文件：`prompt-engineering` YAML 引号、`first-principles` name、`firecrawl-lean` allowed-tools、`using-llm-wiki` 编码和默认 Agent 的嵌套 metadata；`meituan-travel` 按 YAML name 识别。

## 验证记录

| 检查项 | 结果 |
|---|---|
| 后端 Skill 契约专项 | 通过，19 tests |
| 后端全量 `pytest -q` | 通过，769 passed |
| 后端 `ruff check src packages tests` | 通过 |
| 后端扫描实际用户 Skill | 19 个有效 Skill，0 个 diagnostics；包含 `prompt-engineering`、`meituan-travel` |
| 客户端 Skill/Chat 专项 | 通过，21 tests |
| 客户端 renderer 全量 | 通过，61 files / 572 tests |
| 客户端 TypeScript | 通过 |
| 客户端 production build | 通过；仅有既有 CSS、动态 import 和 chunk 大小警告 |
| 两仓库 `git diff --check` | 通过 |
| 审计补丁专项 | 通过，19 tests；提示规则与 `loadSkill` 作用域回归通过 |

## 手工等价验收

1. `~/.ftre/skills/prompt-engineering/SKILL.md` 在 `/api/skills?agent_id=default` 中可见。
2. `~/.ftre/agents/default/skills/meituan-trip/SKILL.md` 以 YAML `name: meituan-travel` 出现在目录中。
3. Codex `r0` 与 Agents `r1` root 继续被扫描，外部来源不会被客户端映射成 global。
4. 根目录无 YAML 的 README 只出现在 diagnostics，不出现在 Skill 列表。
5. Session 切换到其它 Agent 后，输入框和详情请求使用该 Session 的 Agent 作用域。

## 交付状态

C6.6 代码、测试、PRD、TODO 和执行记录已完成。基础提交为后端 `2d2373a`、客户端 `3cf99cb`；本轮审计补丁（提示规则、`loadSkill` 作用域和管理面板作用域提示）尚未提交。工作区原有 F39 变更未被覆盖，未执行 push、merge 或 PR 合入。

## 收尾审计

- 唯一 Owner：`src/ftre/plugins/builtin/skill/service.py` 持有文件发现、解析、winner 和 CRUD；Plugin 只负责装配 HTTP、Tool、Prompt 和 inline handler，没有第二套 Skill 目录解析器。
- 旧规则扫描：生产代码和活动测试中没有 `_IGNORED_ROOT_FILES`、旧解析函数、未参数化的 `fetchSkills()` 或 `ftre_agent_core` 引用；Prompt 的 README 规则已与 frontmatter 资格判定一致。
- 生命周期：Skill Plugin 的 HTTP/Tool/Prompt/inline 注册均绑定 Cordis Fiber disposer；`SkillService.register()` 和 `clear_loaded()` 可重复调用；审计未发现悬挂监听器或重复注册入口。
- 生成物：后端清理 `79` 个 `__pycache__`、`.pytest_cache`、`.ruff_cache`；用户数据、依赖目录、客户端 `dist` 与 `release-f39` 保留。
- 最终工作区：两仓库仍保留原有 F39/发布相关未提交文件，以及本轮审计补丁未提交文件；没有执行 push、merge 或 release。
