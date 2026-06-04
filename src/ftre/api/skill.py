"""
Skill 文件管理 - 让前端/HTTP API 对 ~/.ftre/skills 下的本地 Skill 做 CRUD。

Skill 是存放在 ~/.ftre/skills 下的可复用能力说明，支持两种形态：
- 单文件：  ~/.ftre/skills/<name>.md
- 目录形态：~/.ftre/skills/<name>/SKILL.md（或 skill.md）

与 ~/.ftre/plugins/skill_plugin.py 的加载约定保持一致：插件运行时会把这些
Skill 的描述注入 system_prompt，并提供 loadSkill 工具按需读取完整内容。

本模块只负责文件 IO（与 ftre.tools.cron 的文件 IO 同构），HTTP 路由层
（ftre/api/routes.py）在其上提供 CRUD 接口给前端管理 UI 使用。
"""
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Skill 目录固定位置，与 skill_plugin.py 的 DEFAULT_SKILLS_DIR 对齐
SKILLS_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "skills"

# 新建 Skill 时使用的目录形态文件名（与现有 Skill 生态一致）
SKILL_FILENAME = "SKILL.md"

# 创建新 Skill 时预填的内容模板
SKILL_TEMPLATE = """\
---
name: {name}
description: {description}
---

# {name}

在这里编写 Skill 的工作流程、约束、示例与工具用法。
"""


def _ensure_dir() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def is_valid_name(name: str) -> bool:
    """
    Skill 名称合法性校验：

    - 非空
    - 不含路径分隔符 / 不为 '.' 或 '..'（防目录穿越）
    - 不以 '.' 开头（避免隐藏文件/目录）
    """
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if not name or name in (".", ".."):
        return False
    if name.startswith("."):
        return False
    # Path(name).name 会剥掉任何目录成分，若与原值不同说明含分隔符
    return Path(name).name == name


def find_skill_file(name: str) -> Path | None:
    """按加载优先级找到某个 Skill 的内容文件，找不到返回 None。"""
    candidates = (
        SKILLS_DIR / f"{name}.md",
        SKILLS_DIR / name / "SKILL.md",
        SKILLS_DIR / name / "skill.md",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def skill_exists(name: str) -> bool:
    return find_skill_file(name) is not None


def extract_description(text: str) -> str:
    """从 Skill 正文中提取用于列表展示的简短描述。"""
    text = (text or "").strip()
    if not text:
        return ""

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
        if stripped.startswith("#") or stripped.startswith("---"):
            continue
        paragraph.append(stripped)
    return " ".join(paragraph).strip()


def _extract_frontmatter_description(text: str) -> str:
    """解析 YAML frontmatter 里的 description（支持单行与 '|' 多行块）。"""
    if not text.startswith("---"):
        return ""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""

    i = 1
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == "---":
            break
        if stripped.lower().startswith("description:"):
            value = stripped.split(":", 1)[1].strip()
            # 多行块标量：description: | 或 description: >
            if value in ("|", ">", "|-", ">-", "|+", ">+"):
                block: list[str] = []
                i += 1
                while i < n and lines[i].strip() != "---":
                    block.append(lines[i].strip())
                    i += 1
                return " ".join(s for s in block if s).strip()
            return value.strip('"').strip("'")
        i += 1
    return ""


def _skill_meta(name: str, path: Path) -> dict:
    """根据内容文件构造一个 Skill 元信息 dict（不含 content）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"[skill] 读取失败 {path}: {e}")
        text = ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    # kind: 单文件 <name>.md → "file"；目录内 SKILL.md → "dir"
    kind = "file" if path.parent == SKILLS_DIR else "dir"
    return {
        "name": name,
        "description": extract_description(text),
        "kind": kind,
        "updated_at": mtime,
    }


def list_skills() -> list[dict]:
    """
    列出所有 Skill 的元信息（不含正文），按 name 排序。

    每项：{ name, description, kind, updated_at }
    """
    if not SKILLS_DIR.is_dir():
        return []

    out: list[dict] = []
    seen: set[str] = set()

    # 单文件形态 <name>.md
    for path in sorted(SKILLS_DIR.glob("*.md")):
        name = path.stem
        if name and name not in seen:
            out.append(_skill_meta(name, path))
            seen.add(name)

    # 目录形态 <name>/SKILL.md
    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in seen:
            continue
        skill_file = find_skill_file(entry.name)
        if skill_file is None:
            continue
        out.append(_skill_meta(entry.name, skill_file))
        seen.add(entry.name)

    out.sort(key=lambda s: s["name"].lower())
    return out


def read_skill(name: str) -> dict | None:
    """读取单个 Skill 的完整信息（含 content）。不存在返回 None。"""
    path = find_skill_file(name)
    if path is None:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"[skill] 读取失败 {path}: {e}")
        raise
    meta = _skill_meta(name, path)
    meta["content"] = content
    return meta


def create_skill(name: str, content: str, kind: str = "dir") -> dict:
    """
    创建一个新 Skill。

    kind="dir"（默认）：写入 ~/.ftre/skills/<name>/SKILL.md（与现有 Skill 生态一致）
    kind="file"：       写入 ~/.ftre/skills/<name>.md

    name 已存在时抛 FileExistsError。
    """
    _ensure_dir()
    if skill_exists(name):
        raise FileExistsError(name)

    if kind == "file":
        target = SKILLS_DIR / f"{name}.md"
        target.write_text(content, encoding="utf-8")
    else:
        skill_dir = SKILLS_DIR / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        target = skill_dir / SKILL_FILENAME
        target.write_text(content, encoding="utf-8")

    return read_skill(name)  # type: ignore[return-value]


def update_skill(name: str, content: str) -> dict | None:
    """覆盖写已存在 Skill 的正文。Skill 不存在返回 None。"""
    path = find_skill_file(name)
    if path is None:
        return None
    path.write_text(content, encoding="utf-8")
    return read_skill(name)


def delete_skill(name: str) -> bool:
    """
    删除一个 Skill。

    - 单文件形态：删除 <name>.md
    - 目录形态：删除整个 <name>/ 目录（含 references/scripts 等附属资源）

    删除成功返回 True，Skill 不存在返回 False。
    """
    path = find_skill_file(name)
    if path is None:
        return False

    if path.parent == SKILLS_DIR:
        # 单文件形态
        path.unlink()
        return True

    # 目录形态：删除整个 Skill 目录。再次校验目录确实在 SKILLS_DIR 下，防穿越。
    skill_dir = SKILLS_DIR / name
    try:
        skill_dir_resolved = skill_dir.resolve()
        skills_root_resolved = SKILLS_DIR.resolve()
    except OSError:
        return False
    if skills_root_resolved not in skill_dir_resolved.parents:
        logger.error(f"[skill] 拒绝删除越界目录: {skill_dir}")
        return False
    shutil.rmtree(skill_dir)
    return True
