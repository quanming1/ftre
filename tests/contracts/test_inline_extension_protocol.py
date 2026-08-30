from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cordis import Context
from fastapi import FastAPI
from ftre_agent import (
    AGENT_BEFORE_REASONING_SPEC,
    AgentRegistry,
    BeforeReasoningPayload,
)
from ftre_agent.message import UserMsg
from ftre_agent.tool import ToolContext
from ftre_inline_extension import (
    ExtensionContext,
    ExtensionParser,
    InlineExtensionRegistry,
)
from httpx import ASGITransport, AsyncClient

from ftre.kernel.hooks import HookRuntime
from ftre.plugins.builtin.skill.extension_handler import SkillInlineExtensionHandler
from ftre.plugins.builtin.skill.plugin import apply as apply_skill_plugin
from ftre.plugins.builtin.skill.service import SkillService
from ftre.plugins.builtin.skill.tool import build_load_skill_tool
from ftre.plugins.builtin.skill.types import SkillRecord
from ftre.services.config import ConfigService
from ftre.services.system_prompt import SystemPromptService
from ftre.services.tools import ToolService


def test_parser_round_trips_markdown_like_reference() -> None:
    parser = ExtensionParser()
    text = "请检查 ![ftre:skill](ftre://v1/skill/review-code?path=src&note=%E4%B8%AD%E6%96%87)"

    refs = parser.parse(text)

    assert len(refs) == 1
    assert refs[0].type == "skill"
    assert refs[0].name == "review-code"
    assert refs[0].args == {"path": "src", "note": "中文"}
    assert parser.serialize(refs[0]) == (
        "![ftre:skill](ftre://v1/skill/review-code?note=%E4%B8%AD%E6%96%87&path=src)"
    )


@pytest.mark.parametrize(
    "text",
    [
        "![ftre:skill](ftre://v2/skill/review-code)",
        "![ftre:file](ftre://v1/skill/review-code)",
        "![ftre:skill](https://example.com/review-code)",
        "![ftre:skill](ftre://v1/skill/Review-Code)",
    ],
)
def test_parser_ignores_unsupported_or_malformed_references(text: str) -> None:
    assert ExtensionParser().parse(text) == ()


def test_extension_registry_reports_conflict_and_disposes_cleanly() -> None:
    class Handler:
        type = "skill"

        async def resolve(self, ref, *, context):
            del ref, context
            raise AssertionError("not called")

    registry = InlineExtensionRegistry()
    first = registry.register(Handler(), owner="z-owner", priority=20)
    second = registry.register(Handler(), owner="a-owner", priority=10)

    assert registry.handler_for("skill").__class__ is Handler
    assert registry.diagnostics[0].winner == "a-owner"
    assert registry.diagnostics[0].owners == ("a-owner", "z-owner")
    assert second() is True
    assert registry.diagnostics == ()
    assert first() is True
    assert first() is False


def test_skill_service_crud_and_frontmatter_metadata(tmp_path: Path) -> None:
    service = SkillService(
        {"global": tmp_path / "global", "agent": tmp_path / "agent"}
    )

    created = service.create(
        "review-code",
        "检查代码",
        "代码审查",
        "dir",
    )
    assert created.kind == "dir"
    rows = service.list()
    assert len(rows) == 1
    assert rows[0]["id"] == "review-code"
    assert rows[0]["description"] == "代码审查"
    assert rows[0]["kind"] == "dir"
    assert rows[0]["updated_at"] > 0
    assert rows[0]["disabled"] is False
    assert rows[0]["scope"] == "global"
    assert rows[0]["user_invocable"] is True
    assert rows[0]["model_invocable"] is True

    nested = tmp_path / "global" / "catalog"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: catalog\ndescription: nested\n---\nbody\n", encoding="utf-8"
    )
    (nested / "README.md").write_text("not a skill\n", encoding="utf-8")
    (nested / "references").mkdir()
    (nested / "references" / "details.md").write_text("not a skill\n", encoding="utf-8")
    names = {item["name"] for item in service.list()}
    assert names == {"catalog", "review-code"}

    updated = service.update("review-code", "新的指令")
    assert "新的指令" in updated.content
    service.delete("review-code")
    assert [item["name"] for item in service.list()] == ["catalog"]


def test_skill_detail_exposes_semantic_uri_and_validated_source_capabilities(tmp_path: Path) -> None:
    service = SkillService(
        {
            "global": tmp_path / "global",
            "agent": tmp_path / "agent",
        }
    )
    service.create("review-code", "检查代码", "代码审查", "dir")

    record = service.get("review-code")
    assert record is not None
    detail = service.serialize(record)

    assert detail["uri"] == "ftre://v1/skill/review-code"
    assert detail["media_type"] == "text/markdown"
    assert detail["revision"].startswith("mtime:")
    assert detail["source"] == {
        "kind": "filesystem",
        "path": str((tmp_path / "global" / "review-code" / "SKILL.md").resolve()),
    }
    assert detail["capabilities"] == {"read": True, "browse": True, "write": False}

    runtime_disposer = service.register(
        SkillRecord(
            "runtime-skill",
            "动态内容",
            "runtime",
            "runtime",
            1,
            "runtime",
            "运行时",
        ),
        owner="runtime",
        scope="runtime",
    )
    runtime = service.get("runtime-skill")
    assert runtime is not None
    runtime_detail = service.serialize(runtime)
    assert runtime_detail["source"] == {"kind": "content"}
    assert runtime_detail["capabilities"] == {"read": True, "browse": False, "write": False}
    assert runtime_disposer() is True

    outside = tmp_path / "outside.md"
    outside.write_text("不应暴露为文件路径\n", encoding="utf-8")
    path_runtime_disposer = service.register(
        SkillRecord(
            "path-runtime-skill",
            "动态内容",
            "runtime-plugin",
            "runtime",
            1,
            "runtime",
            "运行时",
            path=str(outside),
        ),
        owner="runtime-plugin",
        scope="runtime",
    )
    path_runtime = service.get("path-runtime-skill")
    assert path_runtime is not None
    path_runtime_detail = service.serialize(path_runtime)
    assert path_runtime_detail["source"] == {"kind": "content"}
    assert path_runtime_detail["capabilities"] == {
        "read": True,
        "browse": False,
        "write": False,
    }
    assert path_runtime_disposer() is True


def test_skill_service_discovery_uses_frontmatter_name_and_rejects_invalid_candidates(tmp_path: Path) -> None:
    root = tmp_path / "global"
    root.mkdir()
    valid = root / "review-code"
    valid.mkdir()
    (valid / "SKILL.md").write_text(
        "---\nname: review-code\ndescription: Review source\n---\nbody\n",
        encoding="utf-8",
    )
    (valid / "README.md").write_text("not a skill\n", encoding="utf-8")
    (valid / "references").mkdir()
    (valid / "references" / "guide.md").write_text("not a skill\n", encoding="utf-8")
    (valid / "scripts").mkdir()
    (valid / "scripts" / "check.py").write_text("print('no')\n", encoding="utf-8")
    (valid / "assets").mkdir()
    (valid / "assets" / "README.md").write_text("not a skill\n", encoding="utf-8")
    aliased = root / "folder-alias"
    aliased.mkdir()
    (aliased / "SKILL.md").write_text(
        "---\nname: canonical-folder-name\ndescription: Canonical YAML name\n---\nbody\n",
        encoding="utf-8",
    )
    root_named_references = root / "references"
    root_named_references.mkdir()
    (root_named_references / "SKILL.md").write_text(
        "---\nname: references-skill\ndescription: A normal folder Skill\n---\nbody\n",
        encoding="utf-8",
    )
    (root / "lint.md").write_text(
        "---\nname: lint\ndescription: Run lint\nuser-invocable: false\n"
        "disable-model-invocation: true\nwhenToUse: CI\nmetadata:\n  owner: qa\n"
        "compatibility: python 3.12\n---\nbody\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("not a skill\n", encoding="utf-8")
    (root / "README.zh.md").write_text("not a skill\n", encoding="utf-8")
    (root / "LICENSE.md").write_text("not a skill\n", encoding="utf-8")
    (root / "reference").mkdir()
    (root / "reference" / "SKILL.md").write_text("not a skill\n", encoding="utf-8")
    (root / "nested" / "example").mkdir(parents=True)
    (root / "nested" / "example" / "SKILL.md").write_text("not a skill\n", encoding="utf-8")
    (root / "missing-frontmatter.md").write_text("plain body\n", encoding="utf-8")
    (root / "bad-name.md").write_text(
        "---\nname: Bad Name\ndescription: invalid\n---\nbody\n", encoding="utf-8"
    )
    (root / "numeric-name.md").write_text(
        "---\nname: 123\ndescription: invalid\n---\nbody\n", encoding="utf-8"
    )
    (root / "mismatch.md").write_text(
        "---\nname: other-name\ndescription: mismatch\n---\nbody\n", encoding="utf-8"
    )
    (root / "bad-bool.md").write_text(
        "---\nname: bad-bool\ndescription: invalid\nuser-invocable: maybe\n---\nbody\n",
        encoding="utf-8",
    )
    (root / "bad-yaml.md").write_text(
        "---\nname: bad-yaml\ndescription: [unterminated\n---\nbody\n", encoding="utf-8"
    )
    (root / "SKILL.md").write_text("---\nname: root\ndescription: ignored\n---\nbody\n", encoding="utf-8")

    service = SkillService({"global": root, "agent": tmp_path / "agent"})
    rows = service.list()

    assert {item["name"] for item in rows} == {
        "canonical-folder-name",
        "lint",
        "other-name",
        "references-skill",
        "review-code",
    }
    assert service.get("review-code").content == "body\n"
    assert service.get("canonical-folder-name").content == "body\n"
    assert service.get("other-name").content == "body\n"
    assert service.get("lint").user_invocable is False
    assert service.get("lint").model_invocable is False
    assert service.get("lint").when_to_use == "CI"
    assert service.get("lint").metadata == {"owner": "qa"}
    updated_alias = service.update("other-name", "changed alias\n")
    assert updated_alias.name == "other-name"
    assert service.get("other-name").content.strip() == "changed alias"
    assert service.get("lint").compatibility == "python 3.12"
    service.update("lint", "changed\n")
    assert service.get("lint").user_invocable is False
    assert service.get("lint").model_invocable is False
    assert service.get("lint").metadata == {"owner": "qa"}
    assert {Path(item["path"]).name for item in service.diagnostics} >= {
        "README.md",
        "missing-frontmatter.md",
        "bad-name.md",
        "numeric-name.md",
        "bad-bool.md",
        "bad-yaml.md",
    }
    assert all("reason" in item and "scope" in item for item in service.diagnostics)


def test_root_readme_with_valid_frontmatter_is_a_flat_skill(tmp_path: Path) -> None:
    root = tmp_path / "global"
    root.mkdir()
    (root / "README.md").write_text(
        "---\nname: readme-skill\ndescription: A documented Skill\n---\nbody\n",
        encoding="utf-8",
    )
    service = SkillService({"global": root, "agent": tmp_path / "agent"})

    rows = service.list()

    assert [item["name"] for item in rows] == ["readme-skill"]
    assert service.diagnostics == ()


def test_skill_service_crud_finds_global_skill_by_yaml_name(tmp_path: Path) -> None:
    root = tmp_path / "global"
    target = root / "storage-alias" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\nname: canonical-name\ndescription: Canonical\n---\nbody\n",
        encoding="utf-8",
    )
    service = SkillService({"global": root, "agent": tmp_path / "agent"})

    updated = service.update("canonical-name", "updated\n")
    assert updated.name == "canonical-name"
    assert service.get("canonical-name").content.strip() == "updated"

    service.delete("canonical-name")
    assert service.get("canonical-name") is None


def test_skill_service_scope_precedence_and_runtime_winner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace_skill = workspace / ".ftre" / "skills" / "shared"
    agent_skill = tmp_path / "agent" / "shared"
    global_skill = tmp_path / "global" / "shared"
    for path, description in (
        (workspace_skill, "workspace"),
        (agent_skill, "agent"),
        (global_skill, "global"),
    ):
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: shared\ndescription: {description}\n---\n{description}\n",
            encoding="utf-8",
        )
    service = SkillService({"global": tmp_path / "global", "agent": tmp_path / "agent"})

    assert service.list("agent-1", str(workspace))[0]["description"] == "workspace"
    workspace_record = service.get("shared", "agent-1", str(workspace))
    assert workspace_record is not None
    workspace_detail = service.serialize(workspace_record)
    assert workspace_detail["source"]["kind"] == "filesystem"
    assert workspace_detail["source"]["path"] == str(
        (workspace / ".ftre" / "skills" / "shared" / "SKILL.md").resolve()
    )
    sources = service.sources("shared", "agent-1", str(workspace))
    assert sources["winner"] == f"workspace:{workspace}"
    assert set(sources["shadowed"]) == {"agent:agent-1", "global"}

    disposer = service.register(
        SkillRecord("shared", "runtime\n", "runtime", "runtime", 1, "runtime", "runtime"),
        owner="runtime",
        scope="runtime",
    )
    assert service.get("shared", "agent-1", str(workspace)).description == "runtime"
    assert disposer() is True
    assert disposer() is False


def test_skill_service_exposes_request_scoped_scan_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = SkillService({"global": tmp_path / "global", "agent": tmp_path / "agent"})

    roots = service.scan_roots("agent-1", str(workspace))

    assert [item["alias"] for item in roots] == ["workspace", "agent", "global"]
    assert roots[0]["scope"] == f"workspace:{workspace}"
    assert roots[0]["path"] == str((workspace / ".ftre" / "skills").resolve())
    assert roots[1]["path"] == str((tmp_path / "agent").resolve())
    assert roots[2]["path"] == str((tmp_path / "global").resolve())
    assert [item["priority"] for item in roots] == [10, 20, 30]


def test_skill_service_supports_codex_and_agents_skill_roots(tmp_path: Path) -> None:
    codex_root = tmp_path / "codex" / "skills"
    agents_root = tmp_path / "agents" / "skills"
    for root, name in ((codex_root, "codex-skill"), (agents_root, "agent-skill")):
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\nbody\n",
            encoding="utf-8",
        )
    service = SkillService(
        {
            "global": tmp_path / "global",
            "agent": tmp_path / "agent",
            "codex": codex_root,
            "agents": agents_root,
        }
    )

    roots = service.scan_roots()
    rows = service.list()

    assert [item["alias"] for item in roots] == ["agent", "global", "r0", "r1"]
    assert {item["name"] for item in rows} == {"codex-skill", "agent-skill"}
    codex_row = next(item for item in rows if item["name"] == "codex-skill")
    assert codex_row["scope"] == "external"
    assert codex_row["origin"] == "external"
    assert codex_row["source"]["path"] == str((codex_root / "codex-skill" / "SKILL.md").resolve())
    assert service.get("codex-skill").content == "body\n"
    assert service.get("agent-skill").content == "body\n"


@pytest.mark.asyncio
async def test_load_skill_tool_uses_the_active_agent_scope(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "private.md").write_text(
        "---\nname: private\ndescription: agent-only\n---\nprivate body\n",
        encoding="utf-8",
    )
    service = SkillService({"global": tmp_path / "global", "agent": agent_root})
    tools = ToolService()
    tools.register(build_load_skill_tool(service), owner="skill")
    view = await tools.prepare_view("agent-1", "session-1")

    result = await view.execute(
        "loadSkill",
        {"name": "private"},
        ToolContext(
            call_id="call-1",
            name="loadSkill",
            arguments={"name": "private"},
            metadata={"agent_id": "agent-1", "workspace": ""},
        ),
    )

    assert result.output == "private body\n"


def test_skill_service_crud_rejects_invalid_shape(tmp_path: Path) -> None:
    service = SkillService({"global": tmp_path / "global"})

    with pytest.raises(ValueError, match="description"):
        service.create("missing-description", "body", "", "file")
    with pytest.raises(ValueError, match="kind"):
        service.create("bad-kind", "body", "description", "other")


@pytest.mark.asyncio
async def test_skill_inline_handler_returns_structured_user_message(tmp_path: Path) -> None:
    service = SkillService({"global": tmp_path / "global", "agent": tmp_path / "agent"})
    service.create("review-code", "检查代码", "代码审查", "file")
    ref = ExtensionParser().parse("![ftre:skill](ftre://v1/skill/review-code)")[0]

    result = await SkillInlineExtensionHandler(service).resolve(
        ref,
        context=ExtensionContext(
            session_id="sess-1",
            request_id="req-1",
            user_message_id="msg-1",
        ),
    )

    assert result.accepted is True
    assert result.message is not None
    assert result.message["metadata"]["source"] == "extension-invocation"
    assert "<skill_content>" in result.message["content"]


@pytest.mark.asyncio
async def test_skill_http_crud_contract(tmp_path: Path) -> None:
    service = SkillService(
        {"global": tmp_path / "global", "agent": tmp_path / "agent"},
        config_service=ConfigService(tmp_path / "config.json", {}),
    )
    from ftre.plugins.builtin.skill.router import build_router

    app = FastAPI()
    app.include_router(build_router(service), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/skills",
            json={"name": "http-skill", "description": "HTTP", "content": "body", "kind": "file"},
        )
        assert created.status_code == 200
        assert created.json()["name"] == "http-skill"
        assert (await client.get("/api/skills")).json()["skills"][0]["description"] == "HTTP"
        (tmp_path / "global" / "broken.md").write_text("broken\n", encoding="utf-8")
        diagnostics = (await client.get("/api/skills/diagnostics")).json()["diagnostics"]
        assert any(item["path"].endswith("broken.md") for item in diagnostics)
        detail = (await client.get("/api/skills/http-skill")).json()
        assert detail["content"].endswith("body\n")
        assert detail["uri"] == "ftre://v1/skill/http-skill"
        assert detail["source"]["kind"] == "filesystem"
        assert detail["capabilities"]["browse"] is True
        assert (await client.put("/api/skills/http-skill", json={"content": "changed"})).status_code == 200
        assert (await client.patch("/api/skills/http-skill/toggle")).json()["disabled"] is True
        assert (await client.delete("/api/skills/http-skill")).status_code == 204
        assert (await client.get("/api/skills/http-skill")).status_code == 404
        assert (
            await client.post(
                "/api/skills",
                json={"name": "missing-description", "content": "body", "kind": "file"},
            )
        ).status_code == 400
        assert (
            await client.post(
                "/api/skills",
                json={"name": "bad-kind", "description": "bad", "content": "body", "kind": "other"},
            )
        ).status_code == 400


class _ToolRegistry:
    def register(self, *_args, **_kwargs):
        return lambda: None


class _RouterRegistry:
    def register_router(self, *_args, **_kwargs):
        return lambda: None


class _Sessions:
    def __init__(self, message: UserMsg):
        self.message = message
        self.updated: list[UserMsg] = []
        self.upserted: list[tuple[str, dict]] = []

    async def get_messages_by_session(self, _session_id):
        return [self.message]

    def record_to_msg(self, record):
        return record

    async def update_message(self, message):
        self.updated.append(message)

    async def upsert_message(self, session_id, message):
        self.upserted.append((session_id, message))

    async def get_session(self, _session_id):
        return {"agent_id": "default", "workspace": ""}

    async def get_session_metadata(self, _session_id):
        return {}


@pytest.mark.asyncio
async def test_skill_plugin_parses_and_injects_once_at_first_reasoning(tmp_path: Path) -> None:
    user = UserMsg(
        content="执行 ![ftre:skill](ftre://v1/skill/review-code)",
        metadata={"request_id": "req-1", "source": "user"},
    )
    sessions = _Sessions(user)
    service = SkillService(
        {
            "global": tmp_path / "global",
            "agent": tmp_path / "agent",
            "codex": tmp_path / "codex-skills",
            "agents": tmp_path / "agents-skills",
        }
    )
    service.create("review-code", "检查代码", "代码审查", "file")

    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("skills", service)
    context.provide("tools", _ToolRegistry())
    context.provide("http", _RouterRegistry())
    context.provide("config", ConfigService(tmp_path / "config.json", {}))
    context.provide("agent_profiles", None)
    context.provide("sessions", sessions)
    context.provide("system_prompt", SystemPromptService())

    await apply_skill_plugin(context)

    payload = BeforeReasoningPayload(
        agent=object(),
        session_id="sess-1",
        turn_id="turn-1",
        iteration=1,
        cancellation=asyncio.Event(),
        request_id="req-1",
    )
    registry = AgentRegistry()
    registry.ensure("default")
    scope = runtime.context_for_scope(registry.scope_carrier("default"))
    first = await runtime.dispatch(AGENT_BEFORE_REASONING_SPEC, payload, context=scope)
    second = await runtime.dispatch(AGENT_BEFORE_REASONING_SPEC, payload, context=scope)

    assert len(first.messages) == 1
    assert first.messages[0]["metadata"]["source"] == "extension-invocation"
    assert list(second.messages) == []
    assert sessions.updated == [user]
    assert len(sessions.upserted) == 1
    assert sessions.upserted[0][1]["id"].startswith("extension_")
    assert user.metadata["extensions"][0]["name"] == "review-code"
    assert len(user.metadata["extension_invocations"]) == 1
    extensions = context.get("inline_extensions")
    assert extensions.handler_for("skill") is not None
    prompt_service = context.get("system_prompt")
    prompt = prompt_service.assemble("default", "sess-1")
    assert "# 使用技能" in prompt
    assert "### 技能根" in prompt
    assert "### 扫描范围" in prompt
    assert "README）只有通过合法 frontmatter 才是 Skill" in prompt
    assert "根目录下的 `SKILL.md` 不作为入口" in prompt
    assert str((tmp_path / "global").resolve()) in prompt
    assert f"`r0` = `{(tmp_path / 'codex-skills').resolve()}" in prompt
    assert f"`r1` = `{(tmp_path / 'agents-skills').resolve()}" in prompt
    assert "<skill-name>/SKILL.md" in prompt
    assert "`review-code`: 代码审查" in prompt
    assert "MUST use it" in prompt
    assert "loadSkill" in prompt

    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup
    assert extensions.handler_for("skill") is None
    assert not any(section.name == "skill-guidance" for section in prompt_service.snapshot())
