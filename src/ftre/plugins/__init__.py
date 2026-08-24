"""Product Plugins loaded by the ftre Composition Root."""
# 产品 Plugin 聚合包：被 Composition Root 通过 Plugin Manifest 装载。
# 每个子包是一个唯一 Plugin Owner（PRD-F14 §5.3），可独立启用/禁用/卸载，
# 卸载后对应 Service/Hook/Tool/Route 完整消失，基础 Agent Turn 不受影响。
# 目录按能力划分：command / channels / mcp / skill / schedule / team /
# trace / session_title / context_govern / plan，详见各子包。
