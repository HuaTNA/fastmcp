"""MCP Skills extension server implementation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import quote

import mcp_types
from mcp.shared.exceptions import MCPError

from fastmcp.exceptions import FastMCPError
from fastmcp.resources.base import Resource
from fastmcp.server.context import Context
from fastmcp.server.extensions import MethodBinding, ServerExtension
from fastmcp.server.middleware.middleware import MiddlewareContext
from fastmcp.server.providers.skills.skill_provider import SkillResource
from fastmcp.skills import (
    SKILLS_EXTENSION_ID,
    GetSkillParams,
    GetSkillResult,
    ListSkillsParams,
    ListSkillsResult,
    ReadResourceDirectoryParams,
    ReadResourceDirectoryResult,
    SkillEntry,
    SkillFile,
    _parse_frontmatter,
    _thaw_json,
)


def _unwrap(resource: Resource) -> Resource:
    """Unwrap mounted resource adapters for internal skill ownership metadata."""
    seen: set[int] = set()
    current = resource
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "_original_resource", None)
        if not isinstance(original, Resource):
            break
        current = original
    return current


def _is_skill_root(resource: Resource) -> bool:
    original = _unwrap(resource)
    return isinstance(original, SkillResource) and original.file_path == "SKILL.md"


def _proxy_backend_uri(resource: Resource) -> str | None:
    original = _unwrap(resource)
    if isinstance(original, SkillResource) or not callable(
        getattr(original, "_get_client", None)
    ):
        return None
    backend_uri = getattr(original, "_backend_uri", None)
    return str(backend_uri or original.uri)


def _map_proxy_entry(
    entry: SkillEntry,
    root: Resource,
) -> SkillEntry:
    root_uri = str(root.uri)
    backend_root = _proxy_backend_uri(root)
    assert backend_root is not None
    backend_base = backend_root.removesuffix("/SKILL.md")
    local_base = root_uri.removesuffix("/SKILL.md")
    files = entry.resources
    if files != "dynamic":
        files = [
            file.model_copy(
                update={"uri": f"{local_base}{file.uri.removeprefix(backend_base)}"}
            )
            for file in files
        ]
    return entry.model_copy(update={"uri": root_uri, "resources": files})


async def _read_manifest_file(server: Any, uri: str) -> bytes | None:
    if await server.get_resource(uri) is None:
        return None
    try:
        result = await server.read_resource(uri, run_middleware=False)
    except (FastMCPError, MCPError):
        return None
    if len(result.contents) != 1:
        return None
    content = result.contents[0].content
    return content.encode("utf-8") if isinstance(content, str) else content


async def _entry(resource: Resource, server: Any) -> SkillEntry | None:
    original = _unwrap(resource)
    assert isinstance(original, SkillResource)
    root_uri = str(resource.uri)
    if original.skill_dynamic:
        return SkillEntry(
            uri=root_uri,
            frontmatter=_thaw_json(original.skill.frontmatter),
            resources="dynamic",
        )
    resources = []
    for path in sorted(original.skill.files):
        uri = _resource_uri(root_uri, path)
        data = await _read_manifest_file(server, uri)
        if data is None:
            return None
        if path == "SKILL.md":
            try:
                if _parse_frontmatter(data) != _thaw_json(original.skill.frontmatter):
                    return None
            except ValueError:
                return None
        resources.append(
            SkillFile(
                uri=uri,
                digest=f"sha256:{hashlib.sha256(data).hexdigest()}",
                size=len(data),
            )
        )
    return SkillEntry(
        uri=root_uri,
        frontmatter=_thaw_json(original.skill.frontmatter),
        resources=resources,
    )


def _resource_uri(root_uri: str, path: str) -> str:
    suffix = "/".join(quote(segment, safe="") for segment in path.split("/"))
    return f"{root_uri.removesuffix('/SKILL.md')}/{suffix}"


def _paginate_snapshot(
    items: list[Any], cursor: str | None, page_size: int | None, *, snapshot: str
) -> tuple[list[Any], str | None]:
    if page_size is None:
        return items, None
    offset = 0
    if cursor is not None:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if data.get("s") != snapshot:
                raise ValueError("cursor belongs to an older Skills snapshot")
            offset = data["o"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("cursor offset must be a non-negative integer")
            if offset >= len(items):
                raise ValueError("cursor offset is outside the current Skills snapshot")
        except (
            AttributeError,
            binascii.Error,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise MCPError(
                code=mcp_types.INVALID_PARAMS,
                message="Invalid Skills pagination cursor",
            ) from exc
        except ValueError as exc:
            raise MCPError(code=mcp_types.INVALID_PARAMS, message=str(exc)) from exc
    page_end = offset + page_size
    page = items[offset:page_end]
    if page_end >= len(items):
        return page, None
    token = base64.urlsafe_b64encode(
        json.dumps({"s": snapshot, "o": page_end}, separators=(",", ":")).encode()
    ).decode()
    return page, token


def _snapshot_digest(method: str, items: list[Any]) -> str:
    serialized = [item.model_dump(mode="json") for item in items]
    data = json.dumps([method, serialized], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


class SkillsExtension(ServerExtension):
    """Serve Skills protocol methods on a FastMCP server."""

    identifier = SKILLS_EXTENSION_ID

    def __init__(self, *, directory_read: bool = True) -> None:
        self.directory_read = directory_read

    def settings(self) -> dict[str, Any]:
        return {"directoryRead": self.directory_read}

    def methods(self) -> tuple[MethodBinding, ...]:
        methods = (
            MethodBinding("skills/list", ListSkillsParams, self._list_skills),
            MethodBinding("skills/get", GetSkillParams, self._get_skill),
        )
        if not self.directory_read:
            return methods
        return (
            *methods,
            MethodBinding(
                "resources/directory/read",
                ReadResourceDirectoryParams,
                self._read_directory,
            ),
        )

    async def _middleware(
        self,
        method: str,
        params: Any,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        server = self.server
        async with Context(fastmcp=server) as context:
            middleware_context = MiddlewareContext(
                message=params,
                source="client",
                type="request",
                method=method,
                fastmcp_context=context,
            )
            return await server._dispatch_component_middleware(
                context=middleware_context,
                call_next=lambda context: call(),
            )

    async def _visible_resources(self) -> list[Resource]:
        # FastMCP applies visibility, session transforms, and per-resource auth.
        return list(await self.server.list_resources(run_middleware=False))

    async def _proxy_entry(
        self, entry: SkillEntry, root: Resource
    ) -> SkillEntry | None:
        mapped = _map_proxy_entry(entry, root)
        if mapped.resources != "dynamic":
            files = []
            for file in mapped.resources:
                data = await _read_manifest_file(self.server, file.uri)
                if data is None:
                    return None
                if file.uri == mapped.uri:
                    try:
                        if _parse_frontmatter(data) != mapped.frontmatter:
                            return None
                    except ValueError:
                        return None
                files.append(
                    file.model_copy(
                        update={
                            "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
                            "size": len(data),
                        }
                    )
                )
            mapped = mapped.model_copy(update={"resources": files})
        return mapped

    async def _list_skills(
        self, ctx: Any, params: ListSkillsParams | None
    ) -> ListSkillsResult:
        async def run() -> ListSkillsResult:
            resources = await self._visible_resources()
            roots = [r for r in resources if _is_skill_root(r)]
            entries = [
                entry
                for resource in roots
                if (entry := await _entry(resource, self.server)) is not None
            ]
            proxy_roots = [
                resource
                for resource in resources
                if _proxy_backend_uri(resource)
                and str(resource.uri).endswith("/SKILL.md")
            ]
            seen_factories: set[int] = set()
            for root in proxy_roots:
                original = _unwrap(root)
                factory = getattr(original, "_client_factory", None)
                if factory is None:
                    continue
                key = id(factory)
                if key in seen_factories:
                    continue
                seen_factories.add(key)
                factory_roots = [
                    candidate
                    for candidate in proxy_roots
                    if getattr(_unwrap(candidate), "_client_factory", None) is factory
                ]
                roots_by_uri: dict[str, list[Resource]] = {}
                for candidate in factory_roots:
                    backend_uri = _proxy_backend_uri(candidate)
                    if backend_uri is not None:
                        roots_by_uri.setdefault(backend_uri, []).append(candidate)
                client = await cast(Any, original)._get_client()
                try:
                    async with client:
                        remote_skills = await client.list_skills()
                except RuntimeError as exc:
                    if "does not advertise" in str(exc):
                        continue
                    raise
                for skill in remote_skills:
                    for root in roots_by_uri.get(skill.uri, []):
                        entry = await self._proxy_entry(skill._entry, root)
                        if entry is not None:
                            entries.append(entry)
            entries.sort(key=lambda entry: entry.uri)
            page, next_cursor = _paginate_snapshot(
                entries,
                params.cursor if params else None,
                self.server._list_page_size,
                snapshot=_snapshot_digest("skills/list", entries),
            )
            return ListSkillsResult(
                skills=page,
                next_cursor=next_cursor,
                ttl_ms=0,
                cache_scope="private",
            )

        return await self._middleware("skills/list", params, run)

    async def _get_skill(self, ctx: Any, params: GetSkillParams) -> GetSkillResult:
        async def run() -> GetSkillResult:
            resource = await self.server.get_resource(params.uri)
            if resource is None:
                raise MCPError(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"No skill is served at {params.uri}",
                )
            if not _is_skill_root(resource):
                backend_uri = _proxy_backend_uri(resource)
                if backend_uri is None or not backend_uri.endswith("/SKILL.md"):
                    raise MCPError(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"No skill is served at {params.uri}",
                    )
                original = _unwrap(resource)
                client = await cast(Any, original)._get_client()
                async with client:
                    result = await client.get_skill_mcp(backend_uri)
                entry = await self._proxy_entry(result.skill, resource)
                if entry is None:
                    raise MCPError(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"No fully readable skill is served at {params.uri}",
                    )
                return GetSkillResult(skill=entry, ttl_ms=0, cache_scope="private")
            entry = await _entry(resource, self.server)
            if entry is None:
                raise MCPError(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"No fully readable skill is served at {params.uri}",
                )
            return GetSkillResult(skill=entry, ttl_ms=0, cache_scope="private")

        return await self._middleware("skills/get", params, run)

    async def _read_directory(
        self, ctx: Any, params: ReadResourceDirectoryParams
    ) -> ReadResourceDirectoryResult:
        async def run() -> ReadResourceDirectoryResult:
            candidate = params.uri.rstrip("/")
            while "://" in candidate:
                root_uri = f"{candidate}/SKILL.md"
                root = await self.server.get_resource(root_uri)
                if root is not None and _is_skill_root(root):
                    original = _unwrap(root)
                    assert isinstance(original, SkillResource)
                    skill = original.skill
                    directory_path = next(
                        (
                            path
                            for path in ("", *skill.directories)
                            if params.uri
                            == (
                                candidate if not path else _resource_uri(root_uri, path)
                            )
                        ),
                        None,
                    )
                    if directory_path is not None:
                        if await _entry(root, self.server) is None:
                            break
                        children: dict[str, mcp_types.Resource] = {}
                        prefix = f"{directory_path}/" if directory_path else ""
                        for file_path in skill.files:
                            if not file_path.startswith(prefix):
                                continue
                            remainder = file_path[len(prefix) :]
                            if not remainder or "/" in remainder:
                                continue
                            uri = _resource_uri(root_uri, file_path)
                            actual = await self.server.get_resource(uri)
                            if actual is not None:
                                children[uri] = actual.to_mcp_resource()
                        for subdir in skill.directories:
                            if not subdir.startswith(prefix):
                                continue
                            remainder = subdir[len(prefix) :]
                            if not remainder or "/" in remainder:
                                continue
                            uri = _resource_uri(root_uri, subdir)
                            children[uri] = mcp_types.Resource(
                                uri=uri, name=remainder, mime_type="inode/directory"
                            )
                        return self._directory_page(params, list(children.values()))
                backend_root = _proxy_backend_uri(root) if root is not None else None
                if (
                    root is not None
                    and backend_root is not None
                    and backend_root.endswith("/SKILL.md")
                ):
                    local_base = str(root.uri).removesuffix("/SKILL.md")
                    backend_base = backend_root.removesuffix("/SKILL.md")
                    upstream_uri = (
                        f"{backend_base}{params.uri.removeprefix(local_base)}"
                    )
                    original = _unwrap(root)
                    client = await cast(Any, original)._get_client()
                    async with client:
                        settings = (client.server_capabilities.extensions or {}).get(
                            SKILLS_EXTENSION_ID, {}
                        )
                        if settings.get("directoryRead", False):
                            result = await client.read_resource_directory_mcp(
                                upstream_uri, cursor=params.cursor
                            )
                            mapped = [
                                item.model_copy(
                                    update={
                                        "uri": f"{local_base}{str(item.uri).removeprefix(backend_base)}"
                                    }
                                )
                                for item in result.resources
                            ]
                            return ReadResourceDirectoryResult(
                                resources=mapped, next_cursor=result.next_cursor
                            )
                        entry = (await client.get_skill_mcp(backend_root)).skill
                    mapped_entry = await self._proxy_entry(entry, root)
                    if mapped_entry is None:
                        break
                    if mapped_entry.resources == "dynamic":
                        paths = [
                            str(resource.uri).removeprefix(f"{local_base}/")
                            for resource in await self._visible_resources()
                            if str(resource.uri).startswith(f"{local_base}/")
                        ]
                    else:
                        paths = [
                            file.uri.removeprefix(f"{local_base}/")
                            for file in mapped_entry.resources
                        ]
                    relative = params.uri.removeprefix(f"{local_base}/")
                    if params.uri != local_base and not any(
                        path.startswith(f"{relative}/") for path in paths
                    ):
                        break
                    children = {}
                    prefix = f"{relative}/" if params.uri != local_base else ""
                    for path in paths:
                        if not path.startswith(prefix):
                            continue
                        remainder = path[len(prefix) :]
                        if not remainder:
                            continue
                        first = remainder.split("/", 1)[0]
                        child_uri = f"{params.uri}/{first}"
                        if "/" in remainder:
                            children[child_uri] = mcp_types.Resource(
                                uri=child_uri, name=first, mime_type="inode/directory"
                            )
                        else:
                            actual = await self.server.get_resource(child_uri)
                            if actual is not None:
                                children[child_uri] = actual.to_mcp_resource()
                    return self._directory_page(params, list(children.values()))
                remainder = candidate.partition("://")[2]
                if "/" not in remainder:
                    break
                candidate = candidate.rsplit("/", 1)[0]
            raise MCPError(
                code=mcp_types.INVALID_PARAMS,
                message=f"No directory is served at {params.uri}",
            )

        return await self._middleware("resources/directory/read", params, run)

    def _directory_page(
        self, params: ReadResourceDirectoryParams, items: list[mcp_types.Resource]
    ) -> ReadResourceDirectoryResult:
        ordered = sorted(items, key=lambda item: str(item.uri))
        page, next_cursor = _paginate_snapshot(
            ordered,
            params.cursor,
            self.server._list_page_size,
            snapshot=_snapshot_digest(f"directory:{params.uri}", ordered),
        )
        return ReadResourceDirectoryResult(resources=page, next_cursor=next_cursor)
