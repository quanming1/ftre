"""Skill catalog and the single filesystem owner for Skill CRUD."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from yaml import YAMLError

from ftre.services.config.loader import load_config_file
from ftre.services.config.paths import CONFIG_PATH

from .types import SkillRecord

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER = re.compile(r"^---(?:\r?\n)(?P<body>.*?)(?:\r?\n)---(?:[ \t]*(?:\r?\n|$))", re.DOTALL)
_FRONTMATTER_BOOL_TRUE = {"true", "yes", "on", "1"}
_FRONTMATTER_BOOL_FALSE = {"false", "no", "off", "0"}
_IGNORED_ROOT_ENTRIES = {".system", "assets", "reference", "references", "scripts"}
_IGNORED_ROOT_FILES = {"readme", "license", "changelog", "reference"}
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024
_MAX_COMPATIBILITY_LENGTH = 500


def _skill_uri(name: str) -> str:
    """Return the stable semantic URI used by messages and preview clients."""
    return f"ftre://v1/skill/{name}"


def _skill_revision(item: SkillRecord) -> str:
    """Return a deterministic detail revision without making callers inspect paths."""
    if item.updated_at > 0:
        return f"mtime:{item.updated_at:.6f}"
    digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _filesystem_source(item: SkillRecord) -> dict[str, str] | None:
    """Expose a filesystem source only while its canonical file still exists."""
    if item.owner != "filesystem" or not item.path:
        return None
    try:
        path = Path(item.path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return {"kind": "filesystem", "path": str(path)} if path.is_file() else None


class _InvalidSkillFrontmatter(ValueError):
    """Raised when a candidate cannot satisfy the Skill frontmatter contract."""


class SkillService:
    """Resolve scoped Skills and own global Skill file mutations.

    Catalog reads are deterministic and read-only. CRUD is deliberately limited to
    the global user root; workspace and agent roots are projections and cannot be
    overwritten through this API.
    """

    key = "skills"

    def __init__(
        self,
        roots: dict[str, Path] | None = None,
        *,
        config_service=None,
        agent_profiles=None,
    ) -> None:
        global_root = CONFIG_PATH.parent / "skills"
        self.roots = (
            dict(roots)
            if roots is not None
            else {
                "global": global_root,
                "codex": Path.home() / ".codex" / "skills",
                "agents": Path.home() / ".agents" / "skills",
            }
        )
        self._config = config_service
        self._agent_profiles = agent_profiles
        self._runtime: list[SkillRecord] = []
        self._loaded: dict[tuple[str, str], str] = {}
        self._diagnostics: list[dict[str, Any]] = []

    @property
    def diagnostics(self) -> tuple[dict[str, Any], ...]:
        """Return the latest discovery diagnostics without exposing mutable state."""
        return tuple(dict(item) for item in self._diagnostics)

    def register(self, skill: SkillRecord, owner: str, scope: str = "global"):
        """Add a runtime contribution and return an idempotent disposer."""
        name = self._validate_discovered_name(skill.name)
        record = SkillRecord(
            name=name,
            content=skill.content,
            owner=owner,
            source=skill.source,
            priority=skill.priority,
            scope=scope,
            description=skill.description,
            kind=skill.kind,
            updated_at=skill.updated_at,
            disabled=skill.disabled,
            user_invocable=skill.user_invocable,
            model_invocable=skill.model_invocable,
            path=skill.path,
            metadata=dict(skill.metadata or {}),
            when_to_use=skill.when_to_use,
            license=skill.license,
            compatibility=skill.compatibility,
            allowed_tools=skill.allowed_tools,
        )
        self._runtime.append(record)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._runtime.remove(record)
            except ValueError:
                return False
            return True

        return dispose

    def list(self, agent_id: str = "default", workspace: str | None = None) -> list[dict[str, Any]]:
        """Return one metadata row per winning Skill in the requested scope."""
        disabled = self._disabled_names(agent_id)
        return [self._summary(item, disabled) for item in self._resolve(agent_id, workspace)]

    def scan_roots(
        self,
        agent_id: str = "default",
        workspace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the ordered filesystem roots used by Skill discovery.

        The snapshot is request-scoped because workspace and agent roots depend on
        the current Agent context. Runtime-registered Skills do not have a
        filesystem root and are therefore not included.
        """
        result: list[dict[str, Any]] = []
        for alias, root, priority, scope in self._root_specs(agent_id, workspace):
            resolved = Path(root).expanduser().resolve()
            result.append(
                {
                    "alias": alias,
                    "scope": scope,
                    "path": str(resolved),
                    "priority": priority,
                    "exists": resolved.is_dir(),
                }
            )
        return result

    def get(
        self,
        name: str,
        agent_id: str = "default",
        workspace: str | None = None,
    ) -> SkillRecord | None:
        """Return the highest-priority Skill winner, including its body."""
        disabled = self._disabled_names(agent_id)
        item = next((item for item in self._resolve(agent_id, workspace) if item.name == name), None)
        if item is None:
            return None
        if item.owner == "filesystem" and item.path:
            loaded = self._record_from_path(
                Path(item.path),
                item.owner,
                item.priority,
                item.scope,
                self._root_for(item, workspace),
                load_body=True,
            )
            if loaded is None:
                return None
            item = loaded
        if item.disabled == (name in disabled):
            return item
        return SkillRecord(
            name=item.name,
            content=item.content,
            owner=item.owner,
            source=item.source,
            priority=item.priority,
            scope=item.scope,
            description=item.description,
            kind=item.kind,
            updated_at=item.updated_at,
            disabled=name in disabled,
            user_invocable=item.user_invocable,
            model_invocable=item.model_invocable,
            path=item.path,
            metadata=dict(item.metadata or {}),
            when_to_use=item.when_to_use,
            license=item.license,
            compatibility=item.compatibility,
            allowed_tools=item.allowed_tools,
        )

    def serialize(self, item: SkillRecord, agent_id: str = "default") -> dict[str, Any]:
        """Create the stable HTTP detail shape without exposing the record object.

        ``uri`` is the portable resource identity.  ``source`` describes the
        capability that the desktop preview may use after the service has
        resolved and validated the winning candidate; callers must not derive
        a filesystem path from the URI themselves.
        """
        data = self._summary(item, self._disabled_names(agent_id))
        data["content"] = item.content
        data["uri"] = _skill_uri(item.name)
        data["media_type"] = "text/markdown"
        data["revision"] = _skill_revision(item)
        source = _filesystem_source(item)
        if source is not None:
            data["source"] = source
            data["capabilities"] = {"read": True, "browse": True, "write": False}
        else:
            data["source"] = {"kind": "content"}
            data["capabilities"] = {"read": True, "browse": False, "write": False}
        data["metadata"] = dict(item.metadata or {})
        if item.when_to_use is not None:
            data["when_to_use"] = item.when_to_use
        if item.license is not None:
            data["license"] = item.license
        if item.compatibility is not None:
            data["compatibility"] = item.compatibility
        if item.allowed_tools is not None:
            data["allowed_tools"] = item.allowed_tools
        return data

    def sources(self, name: str, agent_id: str = "default", workspace: str | None = None) -> dict[str, Any]:
        """Expose all candidates so callers can explain shadowing decisions."""
        candidates = [item for item in self._all(agent_id, workspace) if item.name == name]
        ordered = sorted(candidates, key=lambda item: (item.priority, item.owner))
        return {
            "candidates": [item.__dict__ for item in ordered],
            "winner": ordered[0].scope if ordered else None,
            "shadowed": [item.scope for item in ordered[1:]],
        }

    def mark_loaded(self, session_id: str, name: str, source: str) -> None:
        """Record which source supplied a Skill to a session's runtime."""
        self._loaded[(session_id, name)] = source

    def clear_loaded(self) -> None:
        """Drop process-local load diagnostics when the Plugin is unloaded."""
        self._loaded.clear()

    def create(
        self,
        name: str,
        content: str = "",
        description: str = "",
        kind: str = "dir",
    ) -> SkillRecord:
        """Create a global Skill atomically and return its fresh catalog record."""
        name = self._validate_name(name)
        description = str(description or "").strip()
        if not description:
            raise ValueError("Skill description 不能为空")
        if len(description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError("Skill description 不能超过 1024 个字符")
        if kind not in {"file", "dir"}:
            raise ValueError("Skill kind 只能是 file 或 dir")
        root = self._global_root()
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{name}.md" if kind == "file" else root / name / "SKILL.md"
        self._assert_inside(root, target)
        if (root / f"{name}.md").exists() or (root / name).exists():
            raise FileExistsError(f"Skill 已存在: {name}")
        body = self._with_frontmatter(name, content, description)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(target, body)
        record = self._record_from_path(target, "global", 30, "global", root, load_body=True)
        if record is None:
            raise ValueError("创建的 Skill 未通过 frontmatter 校验")
        return record

    def update(self, name: str, content: str) -> SkillRecord:
        """Replace the global Skill body without changing its storage kind."""
        name = self._validate_name(name)
        root = self._global_root()
        target = self._global_path(root, name)
        if target is None:
            raise FileNotFoundError(f"全局 Skill 不存在: {name}")
        current = self._record_from_path(target, "global", 30, "global")
        if current is None:
            raise ValueError(f"全局 Skill frontmatter 无效: {name}")
        self._write_atomic(
            target,
            self._with_frontmatter(
                name,
                content,
                current.description,
                metadata=current.metadata,
                when_to_use=current.when_to_use,
                license_name=current.license,
                compatibility=current.compatibility,
                allowed_tools=current.allowed_tools,
                user_invocable=current.user_invocable,
                model_invocable=current.model_invocable,
            ),
        )
        record = self._record_from_path(target, "global", 30, "global", root, load_body=True)
        if record is None:
            raise ValueError(f"更新后的 Skill 未通过 frontmatter 校验: {name}")
        return record

    def delete(self, name: str) -> None:
        """Delete one global Skill file or directory after validating its root."""
        name = self._validate_name(name)
        root = self._global_root()
        target = self._global_path(root, name)
        if target is None:
            raise FileNotFoundError(f"全局 Skill 不存在: {name}")
        self._assert_inside(root, target)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    async def toggle_disabled(self, name: str) -> dict[str, Any]:
        """Toggle the global disabled_skills list through ConfigService."""
        name = self._validate_name(name)
        if self._global_path(self._global_root(), name) is None:
            raise FileNotFoundError(f"全局 Skill 不存在: {name}")
        values = self._config.snapshot().value if self._config is not None else load_config_file()
        disabled = values.get("disabled_skills", []) if isinstance(values, dict) else []
        names = {str(item) for item in disabled if isinstance(item, str)}
        if name in names:
            names.remove(name)
            state = False
        else:
            names.add(name)
            state = True
        ordered = sorted(names)
        if self._config is not None:
            await self._config.update({"disabled_skills": ordered})
        else:
            self._write_json_atomic(CONFIG_PATH, {**values, "disabled_skills": ordered})
        return {"name": name, "disabled": state}

    def _all(self, agent_id: str, workspace: str | None) -> list[SkillRecord]:
        self._diagnostics = []
        result = list(self._runtime)
        for _alias, root, priority, scope in self._root_specs(agent_id, workspace):
            if not root.is_dir():
                continue
            root = root.resolve()
            try:
                entries = sorted(root.iterdir(), key=lambda path: path.name)
            except OSError as exc:
                self._diagnose(root, scope, root, f"cannot enumerate Skill root: {exc}")
                continue
            for entry in entries:
                if entry.name.lower() in _IGNORED_ROOT_ENTRIES or _is_ignored_root_file(entry.name):
                    continue
                if entry.is_symlink() and not self._inside(root, entry):
                    self._diagnose(root, scope, entry, "symbolic link escapes Skill root")
                    continue
                if entry.is_dir():
                    path = entry / "SKILL.md"
                    if path.is_file() and path.name == "SKILL.md":
                        record = self._record_from_path(path, "filesystem", priority, scope, root)
                        if record is not None:
                            result.append(record)
                elif entry.is_file() and entry.suffix == ".md" and entry.name != "SKILL.md":
                    record = self._record_from_path(entry, "filesystem", priority, scope, root)
                    if record is not None:
                        result.append(record)
        return result

    def _root_specs(
        self,
        agent_id: str,
        workspace: str | None,
    ) -> list[tuple[str, Path, int, str]]:
        roots: list[tuple[str, Path, int, str]] = []
        if workspace:
            roots.append(
                (
                    "workspace",
                    Path(workspace) / ".ftre" / "skills",
                    10,
                    f"workspace:{workspace}",
                )
            )
        roots.append(
            (
                "agent",
                Path(self.roots.get("agent", CONFIG_PATH.parent / "agents" / agent_id / "skills")),
                20,
                f"agent:{agent_id}",
            )
        )
        roots.append(("global", self._global_root(), 30, "global"))
        codex_root = self.roots.get("codex")
        if codex_root is not None:
            roots.append(("r0", Path(codex_root), 40, "codex-user"))
        agents_root = self.roots.get("agents")
        if agents_root is not None:
            roots.append(("r1", Path(agents_root), 50, "agents-user"))
        return roots

    def _resolve(self, agent_id: str, workspace: str | None) -> list[SkillRecord]:
        result: dict[str, SkillRecord] = {}
        for item in sorted(self._all(agent_id, workspace), key=lambda value: (value.priority, value.owner, value.name)):
            result.setdefault(item.name, item)
        return list(result.values())

    def _record_from_path(
        self,
        path: Path,
        owner: str,
        priority: int,
        scope: str,
        root: Path | None = None,
        load_body: bool = False,
    ) -> SkillRecord | None:
        entry_name = path.name
        declared_name = path.parent.name if entry_name == "SKILL.md" else path.stem
        kind = "dir" if entry_name == "SKILL.md" else "file"
        path = path.resolve()
        if root is not None and not self._inside(root, path):
            self._diagnose(root, scope, path, "path escapes Skill root")
            return None
        try:
            frontmatter, content = _read_skill_file(path, load_body=load_body)
            updated_at = path.stat().st_mtime
        except (OSError, YAMLError, _InvalidSkillFrontmatter, UnicodeError) as exc:
            self._diagnose(root, scope, path, f"cannot read Skill file: {exc}")
            return None
        try:
            name = self._validate_discovered_name(frontmatter.get("name"))
            if declared_name != name:
                raise _InvalidSkillFrontmatter(
                    f"name must match enclosing directory/file basename: {declared_name!r} != {name!r}"
                )
            description = _required_string(frontmatter, "description")
            if len(description) > _MAX_DESCRIPTION_LENGTH:
                raise _InvalidSkillFrontmatter("description exceeds 1024 characters")
            user_invocable = _frontmatter_bool(frontmatter, "user-invocable", True)
            model_invocable = not _frontmatter_bool(frontmatter, "disable-model-invocation", False)
            metadata = _metadata(frontmatter)
            when_to_use = _optional_string(frontmatter, "whenToUse")
            license_name = _optional_string(frontmatter, "license")
            compatibility = _optional_string(frontmatter, "compatibility")
            if compatibility is not None and len(compatibility) > _MAX_COMPATIBILITY_LENGTH:
                raise _InvalidSkillFrontmatter("compatibility exceeds 500 characters")
            allowed_tools = _optional_string(frontmatter, "allowed-tools")
        except (YAMLError, _InvalidSkillFrontmatter, UnicodeError) as exc:
            self._diagnose(root, scope, path, str(exc), exc)
            return None
        return SkillRecord(
            name=name,
            content=content,
            owner=owner,
            source=str(path),
            priority=priority,
            scope=scope,
            description=description,
            kind=kind,
            updated_at=updated_at,
            user_invocable=user_invocable,
            model_invocable=model_invocable,
            path=str(path),
            metadata=metadata,
            when_to_use=when_to_use,
            license=license_name,
            compatibility=compatibility,
            allowed_tools=allowed_tools,
        )

    def _summary(self, item: SkillRecord, disabled: set[str]) -> dict[str, Any]:
        return {
            "id": item.name,
            "name": item.name,
            "uri": _skill_uri(item.name),
            "description": item.description,
            "kind": item.kind,
            "updated_at": item.updated_at,
            "disabled": item.disabled or item.name in disabled,
            "scope": (
                "private"
                if item.scope.startswith(("agent:", "workspace:"))
                else "external"
                if item.scope in {"codex-user", "agents-user"}
                else "global"
            ),
            "user_invocable": item.user_invocable,
            "model_invocable": item.model_invocable,
        }

    def _disabled_names(self, agent_id: str) -> set[str]:
        values = self._config.snapshot().value if self._config is not None else load_config_file()
        names = values.get("disabled_skills", []) if isinstance(values, dict) else []
        disabled = {str(item) for item in names if isinstance(item, str)}
        profile_service = self._agent_profiles
        if profile_service is not None:
            try:
                profile = profile_service.get(agent_id)
                profile_value = getattr(profile, "value", profile)
                disabled.update(str(item) for item in getattr(profile_value, "disabled_skills", []))
            except (AttributeError, FileNotFoundError, TypeError, ValueError):
                pass
        return disabled

    def _global_root(self) -> Path:
        return Path(self.roots.get("global", CONFIG_PATH.parent / "skills")).resolve()

    def _root_for(self, item: SkillRecord, workspace: str | None) -> Path | None:
        if item.scope.startswith("workspace:") and workspace:
            return (Path(workspace) / ".ftre" / "skills").resolve()
        if item.scope.startswith("agent:"):
            agent_id = item.scope.removeprefix("agent:")
            return Path(
                self.roots.get("agent", CONFIG_PATH.parent / "agents" / agent_id / "skills")
            ).resolve()
        if item.scope == "codex-user":
            root = self.roots.get("codex")
            return Path(root).expanduser().resolve() if root is not None else None
        if item.scope == "agents-user":
            root = self.roots.get("agents")
            return Path(root).expanduser().resolve() if root is not None else None
        return self._global_root()

    @staticmethod
    def _inside(root: Path, path: Path) -> bool:
        root = root.resolve()
        target = path.resolve(strict=False)
        return target == root or root in target.parents

    def _diagnose(
        self,
        root: Path | None,
        scope: str,
        path: Path,
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        diagnostic: dict[str, Any] = {
            "root": str(root) if root is not None else "",
            "scope": scope,
            "path": str(path),
            "reason": reason,
        }
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            diagnostic["line"] = mark.line + 1
            diagnostic["column"] = mark.column + 1
        self._diagnostics.append(diagnostic)
        logger.warning("Skill candidate ignored: %s", diagnostic)

    @staticmethod
    def _global_path(root: Path, name: str) -> Path | None:
        file_path = root / f"{name}.md"
        dir_path = root / name / "SKILL.md"
        if file_path.is_file() and dir_path.is_file():
            raise RuntimeError(f"Skill 存储冲突: {name}")
        if file_path.is_file():
            return file_path
        if dir_path.is_file():
            return dir_path
        return None

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = str(name or "").strip().lower()
        if not _SAFE_NAME.fullmatch(normalized):
            raise ValueError("Skill 名称只允许小写字母、数字和短横线")
        return normalized

    @staticmethod
    def _validate_discovered_name(name: Any) -> str:
        if not isinstance(name, str) or not 1 <= len(name) <= _MAX_NAME_LENGTH:
            raise _InvalidSkillFrontmatter("name must be 1-64 characters")
        if not _SAFE_NAME.fullmatch(name):
            raise _InvalidSkillFrontmatter("name must use lowercase kebab-case")
        return name

    @staticmethod
    def _assert_inside(root: Path, path: Path) -> None:
        root = root.resolve()
        target = path.resolve()
        if target != root and root not in target.parents:
            raise ValueError("Skill 路径越界")

    @staticmethod
    def _with_frontmatter(
        name: str,
        content: str,
        description: str,
        *,
        metadata: dict[str, Any] | None = None,
        when_to_use: str | None = None,
        license_name: str | None = None,
        compatibility: str | None = None,
        allowed_tools: str | None = None,
        user_invocable: bool = True,
        model_invocable: bool = True,
    ) -> str:
        body = str(content or "").strip()
        if _FRONTMATTER.match(body):
            existing, _ = _parse_frontmatter(body)
            if existing.get("name") != name:
                raise ValueError("Skill content 的 frontmatter name 必须匹配目标 Skill")
            SkillService._validate_discovered_name(existing.get("name"))
            _required_string(existing, "description")
            _frontmatter_bool(existing, "user-invocable", True)
            _frontmatter_bool(existing, "disable-model-invocation", False)
            _metadata(existing)
            description_value = _required_string(existing, "description")
            if len(description_value) > _MAX_DESCRIPTION_LENGTH:
                raise ValueError("Skill description 不能超过 1024 个字符")
            compatibility_value = _optional_string(existing, "compatibility")
            if compatibility_value is not None and len(compatibility_value) > _MAX_COMPATIBILITY_LENGTH:
                raise ValueError("Skill compatibility 不能超过 500 个字符")
            _optional_string(existing, "license")
            _optional_string(existing, "allowed-tools")
            _optional_string(existing, "whenToUse")
            return body + "\n"
        values: dict[str, Any] = {"name": name, "description": str(description or "")}
        if license_name is not None:
            values["license"] = license_name
        if compatibility is not None:
            values["compatibility"] = compatibility
        if metadata:
            values["metadata"] = dict(metadata)
        if allowed_tools is not None:
            values["allowed-tools"] = allowed_tools
        if when_to_use is not None:
            values["whenToUse"] = when_to_use
        if not user_invocable:
            values["user-invocable"] = False
        if not model_invocable:
            values["disable-model-invocation"] = True
        header = yaml.safe_dump(values, allow_unicode=True, sort_keys=False).rstrip()
        return f"---\n{header}\n---\n\n{body}\n"

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @classmethod
    def _write_json_atomic(cls, path: Path, value: dict[str, Any]) -> None:
        cls._write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_skill_file(path: Path, *, load_body: bool) -> tuple[dict[str, Any], str]:
    """Read frontmatter for discovery and read the body only for an active get()."""
    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
        if first_line.rstrip("\r\n") != "---":
            raise _InvalidSkillFrontmatter("missing YAML frontmatter")
        yaml_lines: list[str] = []
        for line in handle:
            if line.rstrip("\r\n").rstrip(" \t") == "---":
                return _parse_frontmatter_mapping("".join(yaml_lines)), handle.read() if load_body else ""
            yaml_lines.append(line)
    raise _InvalidSkillFrontmatter("missing closing YAML frontmatter delimiter")


def _is_ignored_root_file(name: str) -> bool:
    if not name.lower().endswith(".md"):
        return False
    stem = name[:-3].lower().rstrip(".")
    return stem.split(".", 1)[0] in _IGNORED_ROOT_FILES


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER.match(content)
    if match is None:
        raise _InvalidSkillFrontmatter("missing YAML frontmatter")
    return _parse_frontmatter_mapping(match.group("body")), content[match.end() :]


def _parse_frontmatter_mapping(body: str) -> dict[str, Any]:
    parsed = yaml.safe_load(body)
    if not isinstance(parsed, dict):
        raise _InvalidSkillFrontmatter("frontmatter must be a YAML mapping")
    values: dict[str, Any] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            raise _InvalidSkillFrontmatter("frontmatter keys must be strings")
        values[key] = value
    for legacy in ("disableModelInvocation", "modelInvocable", "userInvocable"):
        if legacy in values:
            raise _InvalidSkillFrontmatter(
                f'frontmatter field "{legacy}" is unsupported; use the hyphenated form'
            )
    return values


def _required_string(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _InvalidSkillFrontmatter(f"frontmatter requires non-empty string: {key}")
    return value.strip() if key == "description" else value


def _optional_string(values: dict[str, Any], key: str) -> str | None:
    if key not in values:
        return None
    value = values[key]
    if not isinstance(value, str):
        raise _InvalidSkillFrontmatter(f"frontmatter field {key} must be a string")
    return value


def _metadata(values: dict[str, Any]) -> dict[str, Any]:
    if "metadata" not in values:
        return {}
    value = values["metadata"]
    if not isinstance(value, dict):
        raise _InvalidSkillFrontmatter("metadata must be a mapping")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise _InvalidSkillFrontmatter("metadata must be a string-to-string mapping")
    return dict(value)


def _frontmatter_bool(values: dict[str, Any], key: str, default: bool) -> bool:
    if key not in values:
        return default
    value = values[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _FRONTMATTER_BOOL_TRUE:
            return True
        if lowered in _FRONTMATTER_BOOL_FALSE:
            return False
    raise _InvalidSkillFrontmatter(f"frontmatter field {key} must be a boolean")


__all__ = ["SkillService"]
