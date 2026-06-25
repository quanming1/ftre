"""
skill_plugin - load local Skills through the plugin tool API.

Skills are local prompt files under ~/.ftre/skills:
- name.md
- name/SKILL.md
- name/skill.md
"""
import os
from pathlib import Path
from xml.sax.saxutils import escape

from ftre.plugin import BEFORE_MESSAGES_BUILD, Plugin
from ftre_agent_core.tool import Tool, ToolParameter


def _read_text_safe(path: Path) -> str:
    """读取文件文本，优先 UTF-8，失败则回退到 GBK。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gbk")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")


DEFAULT_SKILLS_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "skills"


class SkillPlugin(Plugin):
    """Register the loadSkill tool."""

    name = "skill"
    version = "1.0.0"

    def setup(self) -> None:
        cfg = self.api.config or {}
        self._skills_dir = Path(cfg.get("skills_dir") or DEFAULT_SKILLS_DIR)
        self.api.append_system_prompt(
            "Skill 是 ~/.ftre/skills 下的本地能力说明。"
            f"当前电脑上的 Skill 文件夹绝对路径是 {get_skills_dir_path(self._skills_dir)}。"
            "如果用户点名某个 Skill，或用户需求与下方任一 Skill 的能力描述匹配，"
            "且该 Skill 的完整内容尚未在当前对话历史中被加载过，"
            "请调用 loadSkill 读取该 Skill 的完整内容，再按 Skill 内容执行任务。"
            "同一个 Skill 在当前对话中只需加载一次，不要重复加载。"
        )
        self.api.register_hook(BEFORE_MESSAGES_BUILD, self._inject_skill_descriptions)
        self.api.tool_registry.register(create_load_skill_tool(self._skills_dir))

    def _inject_skill_descriptions(self, ctx):
        descriptions = list_skill_descriptions(self._skills_dir)
        if not descriptions:
            return ctx

        lines = [
            "<skill_desc>",
        ]
        for item in descriptions:
            lines.append(
                f"<skill name=\"{escape(item['name'])}\">"
                f"{escape(item['description'])}</skill>"
            )
        lines.append("</skill_desc>")

        current = getattr(ctx.config, "system_prompt", "") or ""
        ctx.config.system_prompt = current.rstrip() + "\n\n" + "\n".join(lines)
        return ctx


def create_load_skill_tool(skills_dir: Path) -> Tool:
    """Create the loadSkill tool."""

    def loadSkill(skill: str) -> str:
        skill_name = (skill or "").strip()
        if not skill_name:
            return "[error] skill name is required"
        if Path(skill_name).name != skill_name:
            return f"[error] invalid skill name: {skill!r}"

        candidates = (
            skills_dir / f"{skill_name}.md",
            skills_dir / skill_name / "SKILL.md",
            skills_dir / skill_name / "skill.md",
        )
        for path in candidates:
            if path.is_file():
                return _read_text_safe(path).strip()
        return f"[error] skill not found: {skill_name}"

    return Tool(
        name="loadSkill",
        description=(
            "加载一个本地 Skill 的说明文本。Skill 是存放在 "
            f"~/.ftre/skills 下的可复用能力说明；当前电脑上的 Skill 文件夹绝对路径是 "
            f"{get_skills_dir_path(skills_dir)}。通常包含某类任务的"
            "工作流程、约束、示例、工具用法或项目约定。"
            "如果用户点名某个 Skill，或用户需求与某个 Skill 的能力描述匹配，"
            "且该 Skill 内容尚未在当前对话中被读取过，请调用本工具读取。"
            "同一个 Skill 在当前对话中只需加载一次，不要重复加载。"
        ),
        parameters=[
            ToolParameter(
                name="skill",
                type="string",
                description="Skill 名称，对应 ~/.ftre/skills/<name>.md 或 ~/.ftre/skills/<name>/SKILL.md",
                required=True,
            ),
        ],
        func=loadSkill,
    )


def get_skills_dir_path(skills_dir: Path | None = None) -> str:
    """Return the absolute local path for the skills directory."""
    path = skills_dir or DEFAULT_SKILLS_DIR
    return str(path.expanduser().resolve())


def list_skill_descriptions(skills_dir: Path) -> list[dict]:
    """Return available skill names and short descriptions."""
    if not skills_dir.is_dir():
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for path in sorted(skills_dir.glob("*.md")):
        name = path.stem
        if name not in seen:
            out.append({"name": name, "description": extract_skill_description(path)})
            seen.add(name)

    for path in sorted(skills_dir.iterdir()):
        if not path.is_dir() or path.name in seen:
            continue
        skill_file = _find_skill_file(path.name, skills_dir)
        if skill_file is None:
            continue
        out.append({"name": path.name, "description": extract_skill_description(skill_file)})
        seen.add(path.name)

    return out


def _find_skill_file(skill_name: str, skills_dir: Path) -> Path | None:
    candidates = (
        skills_dir / f"{skill_name}.md",
        skills_dir / skill_name / "SKILL.md",
        skills_dir / skill_name / "skill.md",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def extract_skill_description(path: Path) -> str:
    """Extract the description portion used for skill discovery."""
    text = _read_text_safe(path).strip()
    if not text:
        return "(no description)"

    frontmatter = _extract_frontmatter_description(text)
    if frontmatter:
        return frontmatter

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")

    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#"):
            continue
        paragraph.append(stripped)
    return " ".join(paragraph).strip() or "(no description)"


def _extract_frontmatter_description(text: str) -> str:
    if not text.startswith("---"):
        return ""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return ""
