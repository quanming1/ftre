"""
skill_plugin - load local Skills through the plugin tool API.

Skills are local prompt files under ~/.ftre/skills:
- name.md
- name/SKILL.md
- name/skill.md
"""
import json
import os
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request

from ftre.plugin import BEFORE_MESSAGES_BUILD, Plugin
from ftre_agent_core.tool import Tool, ToolParameter
from ftre.api import skill as skill_store


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
        self._disabled_skills = self._load_disabled_skills()
        self.api.append_system_prompt(
            "<skill_desc>\n"
            "Skill 是 ~/.ftre/skills 下的本地能力说明。"
            f"当前电脑上的 Skill 文件夹绝对路径是 {get_skills_dir_path(self._skills_dir)}。"
            "如果用户点名某个 Skill，或用户需求与下方任一 Skill 的能力描述匹配，"
            "且该 Skill 的完整内容尚未在当前对话历史中被加载过，"
            "请调用 loadSkill 读取该 Skill 的完整内容，再按 Skill 内容执行任务。"
            "同一个 Skill 在当前对话中只需加载一次，不要重复加载。"
            "\n</skill_desc>"
        )
        self.api.register_hook(BEFORE_MESSAGES_BUILD, self._inject_skill_descriptions)
        self.api.tool_registry.register(create_load_skill_tool(self._skills_dir, self._disabled_skills))
        self.api.register_router(self._build_router())

    def _load_disabled_skills(self) -> set[str]:
        """从 config.json 读取 disabled_skills 数组。"""
        from ftre.config import CONFIG_PATH
        try:
            if not CONFIG_PATH.exists():
                return set()
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            arr = raw.get("disabled_skills", [])
            if isinstance(arr, list):
                return {str(s) for s in arr}
        except Exception:
            pass
        return set()

    def _build_router(self) -> APIRouter:
        """构建 Skill CRUD 路由（迁移自 routes.py）。"""
        router = APIRouter(prefix="/skills")

        @router.get("")
        async def list_skills():
            skills = skill_store.list_skills()
            # 附加 disabled 状态
            for s in skills:
                s["disabled"] = s.get("name", "") in self._disabled_skills
            return {"skills": skills}

        @router.get("/{name}")
        async def get_skill(name: str):
            if not skill_store.is_valid_name(name):
                raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")
            try:
                skill = skill_store.read_skill(name)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"读取失败: {e}")
            if skill is None:
                raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
            return skill

        @router.post("", status_code=201)
        async def create_skill(request: Request):
            try:
                payload = await request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

            name = payload.get("name")
            if not isinstance(name, str) or not skill_store.is_valid_name(name):
                raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name!r}")
            name = name.strip()

            kind = payload.get("kind", "dir")
            if kind not in ("dir", "file"):
                raise HTTPException(status_code=400, detail="kind 仅支持 'dir' / 'file'")

            content = payload.get("content")
            if content is not None and not isinstance(content, str):
                raise HTTPException(status_code=400, detail="content 必须是字符串")
            if not content:
                description = payload.get("description")
                if not isinstance(description, str):
                    description = ""
                content = skill_store.SKILL_TEMPLATE.format(
                    name=name, description=description.strip()
                )

            try:
                skill = skill_store.create_skill(name, content, kind=kind)
            except FileExistsError:
                raise HTTPException(status_code=409, detail=f"Skill 已存在: {name}")
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"创建失败: {e}")
            return skill

        @router.put("/{name}")
        async def update_skill(name: str, request: Request):
            if not skill_store.is_valid_name(name):
                raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")

            try:
                payload = await request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

            content = payload.get("content")
            if not isinstance(content, str):
                raise HTTPException(status_code=400, detail="content 必须是字符串")

            try:
                skill = skill_store.update_skill(name, content)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"保存失败: {e}")
            if skill is None:
                raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
            return skill

        @router.delete("/{name}", status_code=204)
        async def remove_skill(name: str):
            if not skill_store.is_valid_name(name):
                raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")
            try:
                ok = skill_store.delete_skill(name)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"删除失败: {e}")
            if not ok:
                raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
            return None

        @router.patch("/{name}/toggle")
        async def toggle_skill_disabled(name: str):
            """切换 Skill 的禁用状态。"""
            if not skill_store.is_valid_name(name):
                raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")

            from ftre.config import CONFIG_PATH
            import tempfile

            config_data = {}
            if CONFIG_PATH.exists():
                try:
                    config_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                except Exception:
                    config_data = {}

            arr = config_data.get("disabled_skills", [])
            if not isinstance(arr, list):
                arr = []
            arr = [str(s) for s in arr]

            if name in arr:
                arr.remove(name)
                disabled = False
            else:
                arr.append(name)
                disabled = True

            config_data["disabled_skills"] = arr

            # 原子写入
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".config.", suffix=".tmp", dir=str(CONFIG_PATH.parent)
            )
            import os as _os
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, ensure_ascii=False, indent=2)
                _os.replace(tmp_path, CONFIG_PATH)
            except Exception:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            # 更新内存缓存
            self._disabled_skills = set(arr)

            return {"name": name, "disabled": disabled}

        return router

    def _inject_skill_descriptions(self, ctx):
        descriptions = list_skill_descriptions(self._skills_dir)
        if not descriptions:
            return ctx

        # 过滤掉被禁用的 skill
        if self._disabled_skills:
            descriptions = [
                d for d in descriptions if d["name"] not in self._disabled_skills
            ]
        if not descriptions:
            return ctx

        lines = [
            "<skill_list desc=\"以下是你当前可以使用的全部 skill，通过 loadSkill 工具按名称加载对应 skill 后再使用\">",
        ]
        for item in descriptions:
            lines.append(
                f"<skill name=\"{escape(item['name'])}\">"
                f"{escape(item['description'])}</skill>"
            )
        lines.append("</skill_list>")

        current = getattr(ctx.config, "system_prompt", "") or ""
        ctx.config.system_prompt = current.rstrip() + "\n\n" + "\n".join(lines)
        return ctx


def create_load_skill_tool(skills_dir: Path, disabled_skills: set[str] | None = None) -> Tool:
    """Create the loadSkill tool."""

    _disabled = disabled_skills or set()

    def loadSkill(skill: str) -> str:
        skill_name = (skill or "").strip()
        if not skill_name:
            return "[error] skill name is required"
        if Path(skill_name).name != skill_name:
            return f"[error] invalid skill name: {skill!r}"

        if skill_name in _disabled:
            return f"[error] skill '{skill_name}' is disabled"

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
