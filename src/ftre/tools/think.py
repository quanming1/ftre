"""
think 工具 - 内部推理
"""
from ftre_agent_core.tool import Tool, ToolParameter


def create_think_tool() -> Tool:
    """创建 think 工具"""

    def think(thought: str) -> str:
        return thought

    return Tool(
        name="think",
        description="内部思维空间。用来分析问题、制定计划、自我反思。内容不会展示给用户。",
        parameters=[
            ToolParameter(name="thought", type="string", description="你的思考内容", required=True),
        ],
        func=think,
    )
