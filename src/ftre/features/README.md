<!-- 中文说明：Feature 层文档：说明可选产品行为如何消费 Service、贡献工具/路由，并保持独立生命周期。 -->

# features

Feature Plugins own product behavior such as Skill, MCP, Team and Schedule.
They may provide a feature Service, but they do not create a global FastAPI or
reach into AgentLoop/Session private fields.
