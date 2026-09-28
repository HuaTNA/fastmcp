from __future__ import annotations

import pytest
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from fastmcp import Client, Skill
from fastmcp.client.group import ClientGroup
from fastmcp.client.skills import RemoteSkill
from fastmcp.server.providers.skills import SkillProvider
from fastmcp.server.server import FastMCP
from fastmcp.server.skills import SkillsExtension


def _server(name: str, instruction: str) -> tuple[FastMCP, SkillProvider]:
    skill = Skill(
        name=name,
        description=f"A {name} skill.",
        instructions=instruction,
        files={"reference.md": "reference"},
    )
    provider = SkillProvider(skill)
    server = FastMCP(name)
    server.add_provider(provider)
    server.add_extension(SkillsExtension())
    return server, provider


@pytest.mark.anyio
async def test_remote_skill_rejects_content_that_does_not_match_manifest():
    server, provider = _server("refunds", "Use refunds.")
    async with Client(server) as client:
        remote = await client.get_skill("skill://refunds/SKILL.md")
        provider.skill = Skill(
            name="refunds",
            description="A refunds skill.",
            instructions="Changed after discovery.",
            files={"reference.md": "reference"},
        )

        with pytest.raises(ValueError, match="integrity verification failed"):
            await remote.read()


@pytest.mark.anyio
async def test_remote_skill_keeps_verified_snapshot_and_refreshes():
    server, provider = _server("refunds", "Use refunds.")
    async with Client(server) as client:
        remote = await client.get_skill("skill://refunds/SKILL.md")
        original = await remote.read()
        provider.skill = Skill(
            name="refunds",
            description="A refunds skill.",
            instructions="Updated instructions.",
            files={"reference.md": "reference"},
        )

        assert await remote.read() == original
        refreshed = await remote.refresh()
        assert (await refreshed.read()).text.endswith("Updated instructions.")


@pytest.mark.anyio
async def test_remote_skill_manifest_cannot_be_changed_after_discovery():
    server, provider = _server("refunds", "Use refunds.")
    async with Client(server) as client:
        remote = await client.get_skill("skill://refunds/SKILL.md")
        provider.skill = Skill(
            name="refunds",
            description="A refunds skill.",
            instructions="Changed after discovery.",
            files={"reference.md": "reference"},
        )
        with pytest.raises(AttributeError):
            remote.uri = "skill://shipping/SKILL.md"
        assert remote.resources != "dynamic"
        with pytest.raises(ValidationError):
            remote.resources[0].digest = "sha256:" + "0" * 64
        with pytest.raises(ValueError, match="integrity verification failed"):
            await remote.read()


@pytest.mark.anyio
async def test_remote_skill_checks_frontmatter_against_main_file():
    server, _ = _server("refunds", "Use refunds.")
    async with Client(server) as client:
        entry = (await client.get_skill_mcp("skill://refunds/SKILL.md")).skill
        forged = entry.model_copy(
            update={
                "frontmatter": {
                    "name": "refunds",
                    "description": "A forged description.",
                }
            }
        )
        remote = RemoteSkill(client, forged)
        with pytest.raises(ValueError, match="frontmatter verification failed"):
            await remote.read()


@pytest.mark.anyio
async def test_client_requires_server_to_advertise_skills_extension():
    server = FastMCP("ordinary")
    async with Client(server) as client:
        with pytest.raises(RuntimeError, match="does not advertise"):
            await client.list_skills()


@pytest.mark.anyio
async def test_directory_read_requires_advertised_setting():
    server = FastMCP("refunds")
    server.add_provider(
        SkillProvider(
            Skill(
                name="refunds",
                description="A refunds skill.",
                instructions="Use refunds.",
            )
        )
    )
    server.add_extension(SkillsExtension(directory_read=False))

    async with Client(server) as client:
        with pytest.raises(RuntimeError, match="does not support"):
            await client.read_resource_directory("skill://refunds")


@pytest.mark.anyio
async def test_client_group_preserves_skill_origin():
    first, _ = _server("refunds", "Use refunds.")
    second, _ = _server("shipping", "Use shipping.")
    group = ClientGroup({"billing": Client(first), "fulfillment": Client(second)})

    async with group:
        skills = await group.list_skills()
        assert skills["billing"][0].origin == "billing"
        assert skills["fulfillment"][0].origin == "fulfillment"
        skill = await group.get_skill("billing", "skill://refunds/SKILL.md")
        assert (await skill.read()).origin == "billing"
        with pytest.raises(AttributeError):
            setattr(skill, "origin", "fulfillment")


@pytest.mark.anyio
async def test_client_group_keeps_same_skill_uri_bound_to_each_origin():
    billing, _ = _server("refunds", "Use billing.")
    support, _ = _server("refunds", "Use support.")
    group = ClientGroup({"billing": Client(billing), "support": Client(support)})

    async with group:
        for origin, instruction in (
            ("billing", "Use billing."),
            ("support", "Use support."),
        ):
            skill = await group.get_skill(origin, "skill://refunds/SKILL.md")
            content = await skill.read()
            assert content.origin == origin
            assert content.text.endswith(instruction)


@pytest.mark.anyio
async def test_client_can_iterate_skill_pages():
    server = FastMCP("catalog", list_page_size=1)
    server.add_provider(
        SkillProvider(
            Skill(name="refunds", description="Refunds.", instructions="Use refunds.")
        )
    )
    server.add_provider(
        SkillProvider(
            Skill(
                name="shipping", description="Shipping.", instructions="Use shipping."
            )
        )
    )
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        names = [skill.name async for skill in client.iter_skills()]
        assert names == ["refunds", "shipping"]


@pytest.mark.anyio
async def test_dynamic_skill_requires_explicit_unverified_read():
    server = FastMCP("dynamic")
    provider = SkillProvider(
        Skill(name="refunds", description="Refunds.", instructions="Use refunds."),
        dynamic=True,
    )
    server.add_provider(provider)
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        skill = await client.get_skill("skill://refunds/SKILL.md")
        assert skill.resources == "dynamic"
        with pytest.raises(ValueError, match="dynamic resources"):
            await skill.read()
        provider.skill = Skill(
            name="refunds", description="Refunds.", instructions="Use current policy."
        )
        result = await skill.read(allow_unverified=True)
        assert not result.verified
        assert result.text.endswith("Use current policy.")


@pytest.mark.anyio
async def test_skill_list_cursor_is_rejected_after_snapshot_changes():
    server, provider = _server("refunds", "Use refunds.")
    server._list_page_size = 1
    server.add_provider(
        SkillProvider(
            Skill(
                name="shipping", description="Shipping.", instructions="Use shipping."
            )
        )
    )

    async with Client(server) as client:
        first = await client.list_skills_mcp()
        assert first.next_cursor is not None
        provider.skill = Skill(
            name="refunds",
            description="A refunds skill.",
            instructions="Changed content.",
        )
        with pytest.raises(MCPError, match="older Skills snapshot"):
            await client.list_skills_mcp(cursor=first.next_cursor)
