"""Providers for immutable virtual Agent Skills."""

from __future__ import annotations

import mimetypes
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
from pydantic import AnyUrl, Field

from fastmcp.resources.base import Resource
from fastmcp.resources.template import ResourceTemplate
from fastmcp.server.providers.base import Provider
from fastmcp.skills import Skill, _validate_relative_path, _validate_skill_uri
from fastmcp.utilities.authorization import AuthCheck
from fastmcp.utilities.versions import VersionSpec


def skill_file_uri(root_uri: str, relative_path: str) -> str:
    """Return a resource URI under a skill root, quoting each path segment."""
    suffix = "/".join(quote(part, safe="") for part in relative_path.split("/"))
    return f"{root_uri.removesuffix('/SKILL.md')}/{suffix}"


class SkillResource(Resource):
    """A projected resource owned by a virtual skill publication."""

    skill: Any = Field(exclude=True, repr=False)
    skill_uri: str
    file_path: str
    skill_dynamic: bool = False

    async def read(self) -> bytes:
        return self.skill.files[self.file_path]


@dataclass(frozen=True)
class SkillPublication:
    """Bind a storage-neutral skill to its public URI or provider path."""

    skill: Skill
    path: str | None = None
    uri: str | None = None
    auth: AuthCheck | list[AuthCheck] | tuple[AuthCheck, ...] | None = None
    tags: frozenset[str] = frozenset()
    dynamic: bool = False

    def __post_init__(self) -> None:
        if self.path is not None and self.uri is not None:
            raise ValueError("A skill publication accepts either path or uri, not both")
        if self.path is None and self.uri is None:
            object.__setattr__(self, "path", self.skill.name)
        if self.path is not None:
            path = _validate_relative_path(self.path)
            if path.split("/")[-1] != self.skill.name:
                raise ValueError("The final skill path segment must match Skill.name")
            object.__setattr__(self, "path", path)
        else:
            assert self.uri is not None
            uri = _validate_skill_uri(self.uri, self.skill.name)
            object.__setattr__(self, "uri", uri)
        object.__setattr__(self, "tags", frozenset(self.tags))
        if isinstance(self.auth, list):
            object.__setattr__(self, "auth", tuple(self.auth))

    @property
    def root_uri(self) -> str:
        if self.uri is not None:
            return self.uri
        assert self.path is not None
        return f"skill://{self.path}/SKILL.md"


def _resource(
    skill: Skill,
    root_uri: str,
    file_path: str,
    auth: AuthCheck | list[AuthCheck] | None,
    *,
    dynamic: bool = False,
) -> SkillResource:
    mime_type, _ = mimetypes.guess_type(file_path)
    is_main = file_path == "SKILL.md"
    return SkillResource(
        uri=AnyUrl(skill_file_uri(root_uri, file_path)),
        name=skill.name if is_main else f"{skill.name}/{file_path}",
        description=skill.description if is_main else f"File from {skill.name} skill",
        mime_type=(
            "text/markdown" if is_main else mime_type or "application/octet-stream"
        ),
        skill=skill,
        skill_uri=root_uri,
        file_path=file_path,
        skill_dynamic=dynamic,
        auth=auth,
    )


class SkillProvider(Provider):
    """Publish one immutable :class:`Skill` through the resource provider API."""

    def __init__(
        self,
        skill: Skill,
        *,
        path: str | None = None,
        uri: str | None = None,
        dynamic: bool = False,
        auth: AuthCheck | list[AuthCheck] | None = None,
        tags: set[str] | None = None,
    ) -> None:
        super().__init__()
        if path is not None and uri is not None:
            raise ValueError("Pass either path or uri, not both")
        self.skill = skill
        if uri is not None:
            normalized_uri = _validate_skill_uri(uri, skill.name)
            self.path = None
            self._skill_uri = normalized_uri
        else:
            self.path = _validate_relative_path(path or skill.name)
            if self.path.split("/")[-1] != skill.name:
                raise ValueError("The final skill path segment must match Skill.name")
            self._skill_uri = f"skill://{self.path}/SKILL.md"
        self.auth = auth
        self.dynamic = dynamic
        self.tags = tags or set()

    @property
    def skill_uri(self) -> str:
        return self._skill_uri

    def _resources(self) -> list[SkillResource]:
        return [
            _resource(
                self.skill, self.skill_uri, path, self.auth, dynamic=self.dynamic
            ).model_copy(update={"tags": self.tags})
            for path in self.skill.files
        ]

    async def _list_resources(self) -> Sequence[Resource]:
        return self._resources()

    async def _get_resource(
        self, uri: str, version: VersionSpec | None = None
    ) -> Resource | None:
        for resource in self._resources():
            if str(resource.uri) == uri:
                return resource
        return None

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return []

    async def _get_resource_template(
        self, uri: str, version: VersionSpec | None = None
    ) -> ResourceTemplate | None:
        return None


class SkillCatalogProvider(Provider):
    """Publish skills loaded from an application-owned catalog.

    The callbacks can read from a database, object store, or service. Returning
    new :class:`SkillPublication` snapshots on each call keeps refresh policy
    with the application while FastMCP handles resource projection and MCP
    serialization.
    """

    def __init__(
        self,
        list_publications: Callable[[], Awaitable[Sequence[SkillPublication]]],
        *,
        get_publication: Callable[[str], Awaitable[SkillPublication | None]]
        | None = None,
    ) -> None:
        super().__init__()
        self._list_publications_fn = list_publications
        self._get_publication_fn = get_publication

    @staticmethod
    def _provider(publication: SkillPublication) -> SkillProvider:
        auth = (
            list(publication.auth)
            if isinstance(publication.auth, tuple)
            else publication.auth
        )
        return SkillProvider(
            publication.skill,
            path=publication.path,
            uri=publication.uri,
            auth=auth,
            tags=set(publication.tags),
            dynamic=publication.dynamic,
        )

    async def _list_resources(self) -> Sequence[Resource]:
        publications = await self._list_publications_fn()
        return [
            resource
            for publication in publications
            for resource in self._provider(publication)._resources()
        ]

    async def _get_resource(
        self, uri: str, version: VersionSpec | None = None
    ) -> Resource | None:
        if self._get_publication_fn is not None:
            publication = await self._get_publication_fn(uri)
            if publication is None:
                return None
            provider = self._provider(publication)
            return await provider.get_resource(uri, version)
        for resource in await self._list_resources():
            if str(resource.uri) == uri:
                return resource
        return None

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return []

    async def _get_resource_template(
        self, uri: str, version: VersionSpec | None = None
    ) -> ResourceTemplate | None:
        return None


class SkillsDirectoryProvider(Provider):
    """Publish immutable skill snapshots discovered below one or more roots.

    Directory contents remain fixed until :meth:`reload` succeeds. ``reload=True``
    enables development-time rediscovery before each provider request.
    """

    def __init__(
        self,
        roots: str | Path | Sequence[str | Path],
        *,
        reload: bool = False,
        auth: AuthCheck | list[AuthCheck] | None = None,
        tags: set[str] | None = None,
    ) -> None:
        super().__init__()
        if isinstance(roots, str | Path):
            roots = [roots]
        self._roots = tuple(Path(root).resolve() for root in roots)
        if not self._roots:
            raise ValueError("SkillsDirectoryProvider requires at least one root")
        self._reload_enabled = reload
        self._auth = auth
        self._tags = tags or set()
        self._skills: tuple[tuple[str, Skill], ...] = self._scan()
        self._reload_lock = anyio.Lock()

    def _scan(self) -> tuple[tuple[str, Skill], ...]:
        result: list[tuple[str, Skill]] = []
        seen: set[str] = set()
        for root in self._roots:
            if not root.is_dir():
                raise FileNotFoundError(
                    f"Skills root does not exist or is not a directory: {root}"
                )
            for main_file in sorted(root.rglob("SKILL.md")):
                skill_dir = main_file.parent
                skill = Skill.from_directory(skill_dir)
                relative = skill_dir.relative_to(root).as_posix()
                if relative == ".":
                    relative = skill.name
                path = _validate_relative_path(relative)
                if path.split("/")[-1] != skill.name:
                    raise ValueError(
                        f"Skill directory path {path!r} must end in frontmatter name {skill.name!r}"
                    )
                if path in seen:
                    raise ValueError(f"Duplicate skill publication path: {path!r}")
                seen.add(path)
                result.append((path, skill))
        return tuple(result)

    async def reload(self) -> None:
        """Load and atomically publish a complete new directory generation."""
        async with self._reload_lock:
            new_skills = await anyio.to_thread.run_sync(self._scan)
            self._skills = new_skills

    async def _refresh_if_enabled(self) -> None:
        if self._reload_enabled:
            await self.reload()

    def _resources(self) -> list[SkillResource]:
        resources: list[SkillResource] = []
        for path, skill in self._skills:
            root_uri = f"skill://{path}/SKILL.md"
            resources.extend(
                _resource(skill, root_uri, file_path, self._auth).model_copy(
                    update={"tags": self._tags}
                )
                for file_path in skill.files
            )
        return resources

    async def _list_resources(self) -> Sequence[Resource]:
        await self._refresh_if_enabled()
        return self._resources()

    async def _get_resource(
        self, uri: str, version: VersionSpec | None = None
    ) -> Resource | None:
        await self._refresh_if_enabled()
        for resource in self._resources():
            if str(resource.uri) == uri:
                return resource
        return None

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return []

    async def _get_resource_template(
        self, uri: str, version: VersionSpec | None = None
    ) -> ResourceTemplate | None:
        return None

    def __repr__(self) -> str:
        return f"SkillsDirectoryProvider(roots={self._roots!r}, skills={len(self._skills)})"
