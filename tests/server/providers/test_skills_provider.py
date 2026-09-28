from __future__ import annotations

import importlib
from typing import cast

import pytest
from mcp.shared.exceptions import MCPError

from fastmcp import Client, Skill
from fastmcp.resources.base import Resource
from fastmcp.resources.types import TextResource
from fastmcp.server.providers.proxy import ProxyProvider
from fastmcp.server.providers.skills import (
    SkillCatalogProvider,
    SkillProvider,
    SkillPublication,
    SkillsDirectoryProvider,
)
from fastmcp.server.server import FastMCP
from fastmcp.server.skills import SkillsExtension
from fastmcp.server.transforms import Transform


def _write_skill(root, name: str, *, instructions: str = "Use this skill.") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A {name} skill.\n---\n{instructions}",
        encoding="utf-8",
    )


def test_skill_is_a_snapshot_and_validates_files(tmp_path):
    source = tmp_path / "refunds"
    _write_skill(source, "refunds")
    skill_dir = source / "refunds"
    (skill_dir / "policy.md").write_bytes(b"policy\x00")

    skill = Skill.from_directory(skill_dir)
    (skill_dir / "policy.md").write_text("changed", encoding="utf-8")

    assert skill.name == "refunds"
    assert skill.files["policy.md"] == b"policy\x00"
    with pytest.raises(TypeError):
        cast(dict[str, bytes], skill.files)["policy.md"] = b"mutable"


def test_skill_from_package_keeps_exact_file_bytes(tmp_path, monkeypatch):
    package = tmp_path / "example_skill_assets"
    package.mkdir()
    (package / "__init__.py").write_text("")
    _write_skill(package, "refunds")
    (package / "refunds" / "policy.bin").write_bytes(b"\x00\xff")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    skill = Skill.from_package("example_skill_assets", "refunds")
    assert skill.name == "refunds"
    assert skill.files["policy.bin"] == b"\x00\xff"


def test_skill_preserves_frontmatter_bytes_and_rejects_file_directory_conflicts():
    main = b"---\r\nname: raw\r\ndescription: Raw bytes.\r\n---\r\nUse raw bytes.\r\n"
    skill = Skill.from_files({"SKILL.md": main})
    assert skill.files["SKILL.md"] == main

    with pytest.raises(ValueError, match="file is also used as a directory"):
        Skill.from_files(
            {
                "SKILL.md": "---\nname: raw\ndescription: Raw bytes.\n---\nUse raw bytes.",
                "reference": "file",
                "reference/guide.md": "nested",
            }
        )


def test_skill_frontmatter_is_deeply_immutable():
    metadata = {"metadata": {"team": "support"}}
    skill = Skill(
        name="workflow",
        description="A workflow.",
        instructions="Follow the workflow.",
        frontmatter=metadata,
    )
    metadata["metadata"]["team"] = "mutated"

    assert skill.frontmatter["metadata"]["team"] == "support"
    with pytest.raises(TypeError):
        cast(dict[str, str], skill.frontmatter["metadata"])["new"] = "value"


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"compatibility": "x" * 501},
        {"compatibility": None},
        {"metadata": {"tags": ["billing"]}},
        {"metadata": None},
        {"allowed-tools": ["Read"]},
        {"allowed-tools": None},
    ],
)
def test_skill_rejects_invalid_optional_frontmatter(frontmatter):
    with pytest.raises(ValueError):
        Skill(
            name="refunds",
            description="Refunds.",
            instructions="Use refunds.",
            frontmatter=frontmatter,
        )


def test_skill_publication_uri_must_match_skill_name():
    skill = Skill(name="refunds", description="Refunds.", instructions="Use refunds.")
    with pytest.raises(ValueError, match="skill-name"):
        SkillProvider(skill, uri="github://company/repo/other/SKILL.md")
    with pytest.raises(ValueError, match="skill-name"):
        SkillPublication(skill, uri="github://company/repo/other/SKILL.md")


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "a//b", "a\\b", "a/../b"],
)
def test_skill_rejects_unsafe_paths(path):
    with pytest.raises(ValueError, match="Invalid skill path"):
        Skill.from_files(
            {
                "SKILL.md": "---\nname: safe\ndescription: Safe.\n---\nDo this.",
                path: "unsafe",
            }
        )


@pytest.mark.anyio
async def test_skill_extension_list_get_verified_read_and_directory_read():
    skill = Skill(
        name="refunds",
        description="Help with refunds.",
        instructions="Use the refund API.",
        files={
            "references/policy.md": "Refund policy.",
            "assets/pixel.bin": b"\x00\xff",
        },
    )
    server = FastMCP("skills")
    server.add_provider(SkillProvider(skill))
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        listed = await client.list_skills_mcp()
        assert len(listed.skills) == 1
        assert listed.skills[0].uri == "skill://refunds/SKILL.md"
        assert listed.skills[0].resources != "dynamic"

        remote = await client.get_skill("skill://refunds/SKILL.md")
        main = await remote.read()
        assert main.verified
        assert main.text.endswith("Use the refund API.")
        assert main.origin == client.name

        reference = await remote.read("references/policy.md")
        assert reference.verified
        assert reference.data == b"Refund policy."
        binary = await remote.read("assets/pixel.bin")
        assert binary.data == b"\x00\xff"
        assert binary.verified

        directory = await client.read_resource_directory("skill://refunds")
        assert {str(item.uri) for item in directory} == {
            "skill://refunds/SKILL.md",
            "skill://refunds/assets",
            "skill://refunds/references",
        }
        children = await client.read_resource_directory("skill://refunds/references")
        assert [str(item.uri) for item in children] == [
            "skill://refunds/references/policy.md"
        ]


@pytest.mark.anyio
async def test_skills_extension_forwards_through_proxy_provider():
    skill = Skill(
        name="refunds",
        description="Help with refunds.",
        instructions="Use the refund API.",
        files={"references/policy.md": "Refund policy."},
    )
    upstream = FastMCP("upstream")
    upstream.add_provider(SkillProvider(skill))
    upstream.add_extension(SkillsExtension())

    proxy = FastMCP("proxy")
    proxy.add_provider(ProxyProvider(lambda: Client(upstream)), namespace="upstream")
    proxy.add_extension(SkillsExtension())

    async with Client(proxy) as client:
        [remote_skill] = await client.list_skills()
        assert remote_skill.uri == "skill://upstream/refunds/SKILL.md"
        assert (await remote_skill.read()).verified
        directory = await client.read_resource_directory("skill://upstream/refunds")
        assert {str(item.uri) for item in directory} == {
            "skill://upstream/refunds/SKILL.md",
            "skill://upstream/refunds/references",
        }
        policy = await remote_skill.read("references/policy.md")
        assert policy.text == "Refund policy."


@pytest.mark.anyio
async def test_proxy_keeps_same_uri_skills_in_separate_namespaces():
    def upstream(name: str) -> FastMCP:
        server = FastMCP(name)
        server.add_provider(
            SkillProvider(
                Skill(
                    name="refunds",
                    description=f"{name} refunds.",
                    instructions=f"Use {name} policy.",
                    files={"policy.md": f"{name} policy."},
                )
            )
        )
        server.add_extension(SkillsExtension())
        return server

    billing = upstream("billing")
    support = upstream("support")
    proxy = FastMCP("proxy")
    proxy.add_provider(ProxyProvider(lambda: Client(billing)), namespace="billing")
    proxy.add_provider(ProxyProvider(lambda: Client(support)), namespace="support")
    proxy.add_extension(SkillsExtension())

    async with Client(proxy) as client:
        skills = {skill.uri: skill for skill in await client.list_skills()}
        for name in ("billing", "support"):
            skill = skills[f"skill://{name}/refunds/SKILL.md"]
            assert (await skill.read("policy.md")).text == f"{name} policy."


@pytest.mark.anyio
async def test_proxy_lists_same_upstream_mounted_twice():
    upstream = FastMCP("upstream")
    upstream.add_provider(
        SkillProvider(
            Skill(name="refunds", description="Refunds.", instructions="Use refunds.")
        )
    )
    upstream.add_extension(SkillsExtension())

    def client_factory() -> Client:
        return Client(upstream)

    proxy = FastMCP("proxy")
    proxy.add_provider(ProxyProvider(client_factory), namespace="one")
    proxy.add_provider(ProxyProvider(client_factory), namespace="two")
    proxy.add_extension(SkillsExtension())

    async with Client(proxy) as client:
        skills = await client.list_skills()
        assert {skill.uri for skill in skills} == {
            "skill://one/refunds/SKILL.md",
            "skill://two/refunds/SKILL.md",
        }


@pytest.mark.anyio
async def test_proxy_directory_read_works_when_upstream_does_not_advertise_it():
    upstream = FastMCP("upstream")
    upstream.add_provider(
        SkillProvider(
            Skill(
                name="refunds",
                description="Refunds.",
                instructions="Use refunds.",
                files={"policy.md": "Policy."},
            )
        )
    )
    upstream.add_extension(SkillsExtension(directory_read=False))
    proxy = FastMCP("proxy")
    proxy.add_provider(ProxyProvider(lambda: Client(upstream)))
    proxy.add_extension(SkillsExtension())

    async with Client(proxy) as client:
        entries = await client.read_resource_directory("skill://refunds")
        assert {str(item.uri) for item in entries} == {
            "skill://refunds/SKILL.md",
            "skill://refunds/policy.md",
        }


@pytest.mark.anyio
async def test_proxy_directory_read_uses_visible_files_for_dynamic_upstream():
    upstream = FastMCP("upstream")
    upstream.add_provider(
        SkillProvider(
            Skill(
                name="refunds",
                description="Refunds.",
                instructions="Use refunds.",
                files={"references/policy.md": "Policy."},
            ),
            dynamic=True,
        )
    )
    upstream.add_extension(SkillsExtension(directory_read=False))
    proxy = FastMCP("proxy")
    proxy.add_provider(ProxyProvider(lambda: Client(upstream)))
    proxy.add_extension(SkillsExtension())

    async with Client(proxy) as client:
        entries = await client.read_resource_directory("skill://refunds")
        assert {str(item.uri) for item in entries} == {
            "skill://refunds/SKILL.md",
            "skill://refunds/references",
        }


@pytest.mark.anyio
async def test_skills_directory_provider_requires_explicit_reload(tmp_path):
    _write_skill(tmp_path, "alpha")
    provider = SkillsDirectoryProvider(tmp_path)
    server = FastMCP("skills")
    server.add_provider(provider)
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        assert [
            skill.frontmatter["name"]
            for skill in (await client.list_skills_mcp()).skills
        ] == ["alpha"]
        _write_skill(tmp_path, "beta")
        assert [
            skill.frontmatter["name"]
            for skill in (await client.list_skills_mcp()).skills
        ] == ["alpha"]

        await provider.reload()
        assert [
            skill.frontmatter["name"]
            for skill in (await client.list_skills_mcp()).skills
        ] == [
            "alpha",
            "beta",
        ]


@pytest.mark.anyio
async def test_skills_directory_provider_can_auto_reload_for_development(tmp_path):
    _write_skill(tmp_path, "alpha")
    server = FastMCP("skills")
    server.add_provider(SkillsDirectoryProvider(tmp_path, reload=True))
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        _write_skill(tmp_path, "beta")
        skills = await client.list_skills()
        assert {skill.name for skill in skills} == {"alpha", "beta"}


@pytest.mark.anyio
async def test_failed_skills_directory_reload_keeps_previous_snapshot(tmp_path):
    _write_skill(tmp_path, "alpha")
    provider = SkillsDirectoryProvider(tmp_path)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("not frontmatter")

    with pytest.raises(ValueError, match="frontmatter"):
        await provider.reload()

    server = FastMCP("skills")
    server.add_provider(provider)
    server.add_extension(SkillsExtension())
    async with Client(server) as client:
        skills = await client.list_skills()
        assert [skill.name for skill in skills] == ["alpha"]
        assert (await skills[0].read()).verified


@pytest.mark.anyio
async def test_skill_provider_composes_with_namespace_transforms():
    server = FastMCP("skills")
    server.add_provider(
        SkillProvider(
            Skill(name="refunds", description="Refunds.", instructions="Check policy.")
        ),
        namespace="billing",
    )
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        skill = await client.get_skill("skill://billing/refunds/SKILL.md")
        assert skill.name == "refunds"
        assert (await skill.read()).verified


@pytest.mark.anyio
async def test_skill_provider_preserves_custom_uri_through_protocol():
    server = FastMCP("skills")
    server.add_provider(
        SkillProvider(
            Skill(name="refunds", description="Refunds.", instructions="Check policy."),
            uri="catalog://billing/refunds/SKILL.md",
        )
    )
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        skill = await client.get_skill("catalog://billing/refunds/SKILL.md")
        assert skill.uri == "catalog://billing/refunds/SKILL.md"
        assert (await skill.read()).verified


@pytest.mark.anyio
async def test_skill_list_respects_resource_authorization():
    server = FastMCP("skills")
    server.add_provider(
        SkillProvider(
            Skill(
                name="restricted",
                description="Restricted skill.",
                instructions="Do not disclose this skill.",
            ),
            auth=lambda _context: False,
        )
    )
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        assert await client.list_skills() == []


@pytest.mark.anyio
async def test_skill_manifest_does_not_advertise_unreadable_files():
    class HidePolicy(Transform):
        async def list_resources(self, resources):
            return [r for r in resources if not str(r.uri).endswith("/policy.md")]

        async def get_resource(self, uri, call_next, *, version=None):
            if uri.endswith("/policy.md"):
                return None
            return await call_next(uri, version=version)

    server = FastMCP("skills")
    server.add_provider(
        SkillProvider(
            Skill(
                name="refunds",
                description="Refunds.",
                instructions="Use refunds.",
                files={"policy.md": "Restricted policy."},
            )
        )
    )
    server.add_transform(HidePolicy())
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        assert await client.list_skills() == []
        with pytest.raises(MCPError):
            await client.get_skill("skill://refunds/SKILL.md")


@pytest.mark.anyio
async def test_skill_manifest_hashes_content_after_resource_transform():
    class ReplacePolicy(Transform):
        async def get_resource(self, uri, call_next, *, version=None):
            resource = await call_next(uri, version=version)
            if resource is not None and uri.endswith("/policy.md"):
                return TextResource(
                    uri=resource.uri,
                    name=resource.name,
                    text="Current policy.",
                )
            return resource

    server = FastMCP("skills")
    server.add_provider(
        SkillProvider(
            Skill(
                name="refunds",
                description="Refunds.",
                instructions="Use refunds.",
                files={"policy.md": "Original policy."},
            )
        )
    )
    server.add_transform(ReplacePolicy())
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        skill = await client.get_skill("skill://refunds/SKILL.md")
        policy = await skill.read("policy.md")
        assert policy.verified
        assert policy.text == "Current policy."


@pytest.mark.anyio
async def test_skill_with_failing_resource_read_is_not_advertised():
    class UnreadableResource(Resource):
        async def read(self) -> bytes:
            raise PermissionError("not available")

    class ReplacePolicy(Transform):
        async def get_resource(self, uri, call_next, *, version=None):
            resource = await call_next(uri, version=version)
            if resource is not None and uri.endswith("/policy.md"):
                return UnreadableResource(uri=resource.uri, name=resource.name)
            return resource

    server = FastMCP("skills")
    server.add_provider(
        SkillProvider(
            Skill(
                name="refunds",
                description="Refunds.",
                instructions="Use refunds.",
                files={"policy.md": "Policy."},
            )
        )
    )
    server.add_transform(ReplacePolicy())
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        assert await client.list_skills() == []


@pytest.mark.anyio
async def test_application_catalog_publishes_without_a_filesystem_source():
    publication = SkillPublication(
        Skill(
            name="refunds",
            description="Refunds from the billing catalog.",
            instructions="Check the live billing policy.",
            files={"references/policy.md": "Policy stored in the database."},
        ),
        uri="skill://database/refunds/SKILL.md",
    )
    list_calls = 0

    async def list_publications() -> list[SkillPublication]:
        nonlocal list_calls
        list_calls += 1
        return [publication]

    async def get_publication(uri: str) -> SkillPublication | None:
        return publication if uri.startswith("skill://database/refunds/") else None

    server = FastMCP("database skills")
    server.add_provider(
        SkillCatalogProvider(
            list_publications,
            get_publication=get_publication,
        )
    )
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        list_calls = 0
        skill = await client.get_skill("skill://database/refunds/SKILL.md")
        assert list_calls == 0
        policy = await skill.read("references/policy.md")
        assert policy.text == "Policy stored in the database."
        assert list_calls == 0


@pytest.mark.anyio
async def test_directory_read_can_find_skill_omitted_from_catalog_listing():
    publications = {
        name: SkillPublication(
            Skill(
                name=name,
                description=f"{name} skill.",
                instructions=f"Use {name}.",
                files={"guide.md": "Guide."},
            )
        )
        for name in ("alpha", "beta")
    }

    async def list_publications() -> list[SkillPublication]:
        return [publications["alpha"]]

    async def get_publication(uri: str) -> SkillPublication | None:
        for name, publication in publications.items():
            if uri.startswith(f"skill://{name}/"):
                return publication
        return None

    server = FastMCP("catalog")
    server.add_provider(
        SkillCatalogProvider(list_publications, get_publication=get_publication)
    )
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        assert (await client.get_skill("skill://beta/SKILL.md")).name == "beta"
        children = await client.read_resource_directory("skill://beta")
        assert {str(item.uri) for item in children} == {
            "skill://beta/SKILL.md",
            "skill://beta/guide.md",
        }


@pytest.mark.anyio
async def test_nested_skills_are_listed_independently_and_remain_parent_files(tmp_path):
    outer_dir = tmp_path / "outer"
    _write_skill(tmp_path, "outer")
    nested_root = outer_dir / "nested"
    nested_root.mkdir()
    _write_skill(nested_root, "inner")
    provider = SkillsDirectoryProvider(tmp_path)
    server = FastMCP("skills")
    server.add_provider(provider)
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        skills = await client.list_skills()
        assert {skill.name for skill in skills} == {"outer", "inner"}
        outer = next(skill for skill in skills if skill.name == "outer")
        nested_file = await outer.read("nested/inner/SKILL.md")
        assert nested_file.verified


@pytest.mark.anyio
async def test_skill_directory_read_includes_empty_nested_directories():
    skill = Skill(
        name="refunds",
        description="Refunds.",
        instructions="Use refunds.",
        directories=("empty/nested",),
    )
    server = FastMCP("skills")
    server.add_provider(SkillProvider(skill))
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        root = await client.read_resource_directory("skill://refunds")
        assert {str(item.uri) for item in root} == {
            "skill://refunds/SKILL.md",
            "skill://refunds/empty",
        }
        nested = await client.read_resource_directory("skill://refunds/empty")
        assert [str(item.uri) for item in nested] == ["skill://refunds/empty/nested"]
