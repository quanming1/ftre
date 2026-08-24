# 执行提示词 04：Core C3 验收、Wheel 与可安装版本边界

你正在 `E:\ftre-agent-core` 收尾 C3。重新审计前面实现，不默认它们正确。未经用户明确授权，
本批不 push、不创建 PR、不打 tag、不上传 PyPI；但必须产出可复现的 release candidate wheel 和证据。

## 一、全盘审计

- Core 公共 Hook 精确为 5 个：tool/before、tool/after、llm/stream、agent/before-reasoning、
  agent/stop-decision；
- 四个 tools/* 旧名和 turn-stopping 全盘清零；
- 无 ftre/Cordis/Session/Inbox/Compaction/Channel import；
- 无 alias、Bridge、重复 DTO、Port、Facade、第二 Tool 执行器；
- 取消、权限、并发、错误归一化、Tracer/Event 和无状态不变量保持；
- README、API 导出、类型注释、版本元数据和 changelog 一致。

逐个通读改动文件：中文注释应解释 Hook 边界、取消、错误和结果归一化原因，不保留逐行翻译、
旧协议说明或注释掉的实现。清理死代码、未使用 import/helper、缓存、build/dist/egg-info、临时
脚本和调试输出；构建前后的必要产物要区分，不能把 release candidate wheel 误当垃圾删除。

## 二、验证与构建

执行全部专项、全量 pytest、ruff、diff check、wheel/sdist build、包内容检查、临时洁净 venv 安装和
import-origin 验证。wheel 不包含缓存、临时数据、测试数据库或 sibling 源码。记录文件名、版本和哈希。

## 三、文档与提交

逐条核对 C3 FR/AC；全部通过才更新 PRD 为待发布/已实现状态和 TODO 子任务。执行报告列出命令、
测试数、构建物、哈希、删除清单、API diff 和 ftre 所需版本下限。按审计修复/测试/文档分批 commit。

若 ftre CI 需要一个尚未发布的新版本，明确停止在“发行授权”边界，向用户报告需要 push/PR/tag/PyPI
中的哪项授权；不得改用本地路径、临时 sys.path 或伪造版本继续。获得明确授权后才执行仓库规范的
PR/发布流程，并把不可变版本/commit 提供给提示词 05。
