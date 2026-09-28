"""Client APIs for discovering and reading MCP Skills."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import mcp_types
from mcp.client.caching import CacheMode

from fastmcp.client.telemetry import client_span
from fastmcp.skills import (
    MAX_SKILL_BYTES,
    SKILLS_EXTENSION_ID,
    GetSkillParams,
    GetSkillRequest,
    GetSkillResult,
    ListSkillsParams,
    ListSkillsRequest,
    ListSkillsResult,
    ReadResourceDirectoryParams,
    ReadResourceDirectoryRequest,
    ReadResourceDirectoryResult,
    SkillEntry,
    _freeze_json,
    _parse_frontmatter,
    _thaw_json,
    _validate_relative_path,
)

if TYPE_CHECKING:
    from fastmcp.client.client import Client


@dataclass(frozen=True)
class SkillContent:
    """A skill file's verified bytes and resource metadata."""

    data: bytes
    mime_type: str
    uri: str
    skill_uri: str
    origin: str
    verified: bool

    @property
    def text(self) -> str:
        return self.data.decode("utf-8")


class RemoteSkill:
    """An immutable remote skill entry bound to its originating client."""

    __slots__ = (
        "_client",
        "_content_cache",
        "_content_cache_size",
        "_entry",
        "_origin",
        "_sealed",
        "description",
        "frontmatter",
        "name",
        "resources",
        "uri",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("RemoteSkill entries are immutable; use refresh()")
        object.__setattr__(self, name, value)

    def __init__(
        self, client: Client, entry: SkillEntry, *, origin: str | None = None
    ) -> None:
        self._client = client
        self._entry = entry.model_copy(deep=True)
        self.uri = self._entry.uri
        self.frontmatter = _freeze_json(self._entry.frontmatter)
        self.name = str(self._entry.frontmatter["name"])
        self.description = str(self._entry.frontmatter["description"])
        self._origin = origin or client.name
        self.resources = (
            self._entry.resources
            if self._entry.resources == "dynamic"
            else tuple(self._entry.resources)
        )
        self._content_cache: dict[str, SkillContent] = {}
        self._content_cache_size = 0
        self._sealed = True

    @property
    def origin(self) -> str:
        return self._origin

    def _with_origin(self, origin: str) -> RemoteSkill:
        return RemoteSkill(self._client, self._entry, origin=origin)

    @property
    def files(self) -> tuple[str, ...] | str:
        if self.resources == "dynamic":
            return "dynamic"
        return tuple(resource.uri for resource in self.resources)

    async def refresh(self, *, cache_mode: CacheMode = "refresh") -> RemoteSkill:
        skill = await self._client.get_skill(self.uri, cache_mode=cache_mode)
        return skill._with_origin(self.origin)

    async def read(
        self,
        relative_path: str = "SKILL.md",
        *,
        allow_unverified: bool = False,
    ) -> SkillContent:
        if relative_path != "SKILL.md":
            relative_path = _validate_relative_path(relative_path)
        uri = _skill_file_uri(self.uri, relative_path)
        cached = self._content_cache.get(uri)
        if cached is not None:
            return cached
        expected = None
        if self.resources == "dynamic":
            if not allow_unverified:
                raise ValueError(
                    "This skill has dynamic resources; pass allow_unverified=True "
                    "to read content without manifest verification"
                )
        else:
            expected = next((file for file in self.resources if file.uri == uri), None)
            if expected is None:
                raise ValueError(f"{uri!r} is not listed in this skill's manifest")

        with client_span(
            "resources/read",
            "resources/read",
            uri,
            session_id=self._client.transport.get_session_id(),
            resource_uri=uri,
        ):
            result = await self._client.read_resource_mcp(uri)
        if len(result.contents) != 1:
            raise ValueError(f"Expected one content item when reading {uri!r}")
        content = result.contents[0]
        if isinstance(content, mcp_types.TextResourceContents):
            data = content.text.encode("utf-8")
            mime_type = content.mime_type or "text/plain"
        elif isinstance(content, mcp_types.BlobResourceContents):
            try:
                data = base64.b64decode(content.blob, validate=True)
            except ValueError as exc:
                raise ValueError(f"Invalid base64 content for {uri!r}") from exc
            mime_type = content.mime_type or "application/octet-stream"
        else:
            raise ValueError(f"Unsupported resource content for {uri!r}")

        verified = expected is not None
        if expected is not None:
            digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
            if len(data) != expected.size or digest != expected.digest:
                raise ValueError(f"Skill integrity verification failed for {uri!r}")
        if relative_path == "SKILL.md":
            frontmatter = _parse_frontmatter(data)
            if frontmatter != _thaw_json(self.frontmatter):
                raise ValueError(f"Skill frontmatter verification failed for {uri!r}")
        content_value = SkillContent(
            data=data,
            mime_type=mime_type,
            uri=uri,
            skill_uri=self.uri,
            origin=self.origin,
            verified=verified,
        )
        if (
            expected is not None
            and self._content_cache_size + len(data) <= MAX_SKILL_BYTES
        ):
            self._content_cache[uri] = content_value
            object.__setattr__(
                self, "_content_cache_size", self._content_cache_size + len(data)
            )
        return content_value


def _skill_file_uri(skill_uri: str, path: str) -> str:
    suffix = "/".join(quote(segment, safe="") for segment in path.split("/"))
    return f"{skill_uri.removesuffix('/SKILL.md')}/{suffix}"


class ClientSkillsMixin:
    """Extension-aware Skills methods for :class:`fastmcp.Client`."""

    def _require_skills_extension(
        self: Client, *, directory_read: bool = False
    ) -> None:
        capabilities = self.server_capabilities
        settings = (
            (capabilities.extensions or {}).get(SKILLS_EXTENSION_ID)
            if capabilities is not None
            else None
        )
        if settings is None:
            raise RuntimeError(
                f"Server {self.name!r} does not advertise {SKILLS_EXTENSION_ID!r}"
            )
        if directory_read and not settings.get("directoryRead", False):
            raise RuntimeError(
                f"Server {self.name!r} does not support resources/directory/read"
            )

    async def list_skills_mcp(
        self: Client,
        *,
        cursor: str | None = None,
        cache_mode: CacheMode = "use",
    ) -> ListSkillsResult:
        self._require_skills_extension()
        params = ListSkillsParams(cursor=cursor) if cursor is not None else None

        async def send() -> ListSkillsResult:
            return await self._await_with_session_monitoring(
                self.session.send_request(
                    ListSkillsRequest(params=params), ListSkillsResult
                )
            )

        return await self._cached_fetch(
            "skills/list", cursor=cursor, cache_mode=cache_mode, send=send
        )

    async def list_skills(
        self: Client,
        *,
        max_pages: int = 250,
        cache_mode: CacheMode = "use",
    ) -> list[RemoteSkill]:
        return [
            skill
            async for skill in self.iter_skills(
                max_pages=max_pages, cache_mode=cache_mode
            )
        ]

    async def iter_skills(
        self: Client,
        *,
        max_pages: int = 250,
        cache_mode: CacheMode = "use",
    ) -> AsyncIterator[RemoteSkill]:
        """Yield skills page by page without materializing the full catalog."""
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            result = await self.list_skills_mcp(cursor=cursor, cache_mode=cache_mode)
            for entry in result.skills:
                yield RemoteSkill(self, entry)
            if result.next_cursor is None:
                return
            if result.next_cursor in seen:
                raise RuntimeError(
                    "Server returned a repeated skills pagination cursor"
                )
            seen.add(result.next_cursor)
            cursor = result.next_cursor
        raise RuntimeError(
            f"Reached skills auto-pagination limit ({max_pages} pages); "
            "use list_skills_mcp() to page manually"
        )

    async def get_skill_mcp(
        self: Client,
        uri: str,
        *,
        cache_mode: CacheMode = "use",
    ) -> GetSkillResult:
        self._require_skills_extension()

        async def send() -> GetSkillResult:
            return await self._await_with_session_monitoring(
                self.session.send_request(
                    GetSkillRequest(params=GetSkillParams(uri=uri)), GetSkillResult
                )
            )

        return await self._cached_fetch(
            "skills/get",
            cursor=None,
            params_key=uri,
            cache_mode=cache_mode,
            send=send,
        )

    async def get_skill(
        self: Client,
        uri: str,
        *,
        cache_mode: CacheMode = "use",
    ) -> RemoteSkill:
        return RemoteSkill(
            self,
            (await self.get_skill_mcp(uri, cache_mode=cache_mode)).skill,
        )

    async def read_resource_directory_mcp(
        self: Client,
        uri: str,
        *,
        cursor: str | None = None,
    ) -> ReadResourceDirectoryResult:
        self._require_skills_extension(directory_read=True)
        params = ReadResourceDirectoryParams(uri=uri, cursor=cursor)
        return await self._await_with_session_monitoring(
            self.session.send_request(
                ReadResourceDirectoryRequest(params=params),
                ReadResourceDirectoryResult,
            )
        )

    async def read_resource_directory(
        self: Client, uri: str, *, max_pages: int = 250
    ) -> list[mcp_types.Resource]:
        resources: list[mcp_types.Resource] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            result = await self.read_resource_directory_mcp(uri, cursor=cursor)
            resources.extend(result.resources)
            if result.next_cursor is None:
                return resources
            if result.next_cursor in seen:
                raise RuntimeError(
                    "Server returned a repeated directory pagination cursor"
                )
            seen.add(result.next_cursor)
            cursor = result.next_cursor
        raise RuntimeError(
            f"Reached directory auto-pagination limit ({max_pages} pages)"
        )
