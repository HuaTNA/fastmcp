"""Virtual Agent Skills that can be published by FastMCP providers."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import mcp_types
from pydantic import AnyUrl, BaseModel, ConfigDict, Field

MAX_SKILL_FILES = 512
MAX_SKILL_BYTES = 16 * 1024 * 1024
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"


class SkillFile(BaseModel):
    """A file and its exact-byte integrity information."""

    model_config = ConfigDict(frozen=True)

    uri: str
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size: int = Field(ge=0)


class SkillEntry(BaseModel):
    """A protocol entry for a published skill."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    frontmatter: dict[str, Any]
    resources: list[SkillFile] | Literal["dynamic"]


class ListSkillsParams(mcp_types.PaginatedRequestParams):
    pass


class GetSkillParams(mcp_types.RequestParams):
    uri: str


class ReadResourceDirectoryParams(mcp_types.PaginatedRequestParams):
    uri: str


class ListSkillsResult(mcp_types.CacheableResult):
    skills: list[SkillEntry]
    next_cursor: str | None = None
    result_type: Literal["complete"] = "complete"


class GetSkillResult(mcp_types.CacheableResult):
    skill: SkillEntry
    result_type: Literal["complete"] = "complete"


class ReadResourceDirectoryResult(mcp_types.PaginatedResult):
    resources: list[mcp_types.Resource]
    result_type: Literal["complete"] = "complete"


class ListSkillsRequest(mcp_types.PaginatedRequest[Literal["skills/list"]]):
    method: Literal["skills/list"] = "skills/list"


class GetSkillRequest(mcp_types.Request[GetSkillParams, Literal["skills/get"]]):
    method: Literal["skills/get"] = "skills/get"


class ReadResourceDirectoryRequest(
    mcp_types.Request[ReadResourceDirectoryParams, Literal["resources/directory/read"]]
):
    method: Literal["resources/directory/read"] = "resources/directory/read"


def _validate_relative_path(path: str) -> str:
    if not path or "\\" in path or "\x00" in path:
        raise ValueError(f"Invalid skill path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid skill path: {path!r}")
    if path.startswith("/"):
        raise ValueError(f"Skill paths must be relative: {path!r}")
    return "/".join(parts)


def _json_value(value: Any, *, location: str) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item, location=location) for item in value]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {
            key: _json_value(item, location=location) for key, item in value.items()
        }
    raise ValueError(f"Skill frontmatter at {location} must contain JSON values")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _parse_frontmatter(raw: bytes) -> dict[str, Any]:
    import yaml
    import yaml.resolver

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md must be UTF-8") from exc
    text = text.removeprefix("\ufeff")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    match = re.search(r"\r?\n---(?:\r?\n|$)", text[3:])
    if match is None:
        raise ValueError("SKILL.md frontmatter is missing its closing delimiter")
    source = text[3 : 3 + match.start()]

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(
        loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
    ) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("Skill frontmatter keys must be strings")
            if key in mapping:
                raise ValueError(f"Duplicate skill frontmatter key: {key!r}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        parsed = yaml.load(source, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"Invalid SKILL.md YAML frontmatter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    result = _json_value(parsed, location="SKILL.md")
    name = result.get("name")
    description = result.get("description")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or len(name) > 64:
        raise ValueError(
            "Skill name must be 1-64 lowercase letters, digits, or hyphens"
        )
    if not isinstance(description, str) or not description or len(description) > 1024:
        raise ValueError("Skill description must contain 1-1024 characters")
    compatibility = result.get("compatibility")
    if "compatibility" in result and (
        not isinstance(compatibility, str) or len(compatibility) > 500
    ):
        raise ValueError(
            "Skill compatibility must be a string of at most 500 characters"
        )
    metadata = result.get("metadata")
    if "metadata" in result and (
        not isinstance(metadata, dict)
        or not all(isinstance(value, str) for value in metadata.values())
    ):
        raise ValueError("Skill metadata must map strings to strings")
    allowed_tools = result.get("allowed-tools")
    if "allowed-tools" in result and not isinstance(allowed_tools, str):
        raise ValueError("Skill allowed-tools must be a string")
    return result


def _validate_skill_uri(uri: str, name: str) -> str:
    from urllib.parse import unquote, urlsplit

    normalized = str(AnyUrl(uri))
    parts = urlsplit(normalized)
    path = [parts.netloc, *parts.path.strip("/").split("/")]
    if (
        parts.query
        or parts.fragment
        or len(path) < 2
        or path[-1] != "SKILL.md"
        or unquote(path[-2]) != name
    ):
        raise ValueError("A skill URI must end in /<skill-name>/SKILL.md")
    return normalized


@dataclass(frozen=True, init=False)
class Skill:
    """An immutable skill bundle, independent of its storage and published URI.

    Use the keyword constructor to author a skill in Python, or use
    :meth:`from_directory`, :meth:`from_package`, or :meth:`from_files` to
    construct a snapshot from another source.
    """

    name: str
    description: str
    frontmatter: Mapping[str, Any]
    files: Mapping[str, bytes]
    directories: frozenset[str]

    def __init__(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        files: Mapping[str, str | bytes] | None = None,
        directories: Iterable[str] = (),
        frontmatter: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or len(name) > 64:
            raise ValueError(
                "Skill name must be 1-64 lowercase letters, digits, or hyphens"
            )
        if (
            not isinstance(description, str)
            or not description
            or len(description) > 1024
        ):
            raise ValueError("Skill description must contain 1-1024 characters")
        if not isinstance(instructions, str):
            raise ValueError("Skill instructions must be text")
        metadata = dict(frontmatter or {})
        for key in ("name", "description"):
            if key in metadata:
                raise ValueError(f"Pass {key!r} as a top-level Skill argument")
        metadata = {"name": name, "description": description, **metadata}
        metadata = _thaw_json(_json_value(metadata, location="frontmatter"))
        import yaml

        main = (
            "---\n"
            + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
            + "---\n"
            + instructions
        )
        normalized = {"SKILL.md": main.encode("utf-8")}
        for path, content in (files or {}).items():
            clean = _validate_relative_path(path)
            if clean == "SKILL.md":
                raise ValueError("Use `instructions` to create SKILL.md")
            normalized[clean] = (
                content.encode("utf-8") if isinstance(content, str) else bytes(content)
            )
        self._initialize(normalized, directories)

    def _initialize(
        self, files: Mapping[str, bytes], directories: Iterable[str]
    ) -> None:
        normalized: dict[str, bytes] = {}
        for path, content in files.items():
            clean = _validate_relative_path(path)
            if clean in normalized:
                raise ValueError(f"Duplicate skill file path: {clean!r}")
            normalized[clean] = bytes(content)
        if "SKILL.md" not in normalized:
            raise ValueError("A skill must contain SKILL.md")
        frontmatter = _parse_frontmatter(normalized["SKILL.md"])
        if len(normalized) > MAX_SKILL_FILES:
            raise ValueError(f"A skill may contain at most {MAX_SKILL_FILES} files")
        total_size = sum(map(len, normalized.values()))
        if total_size > MAX_SKILL_BYTES:
            raise ValueError(f"A skill may contain at most {MAX_SKILL_BYTES} bytes")
        dirs: set[str] = set()
        for raw_dir in directories:
            directory = _validate_relative_path(raw_dir)
            parts = directory.split("/")
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        for path in normalized:
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        for path in normalized:
            parts = path.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) in normalized:
                    raise ValueError(
                        f"Skill file is also used as a directory: {path!r}"
                    )
        if dirs.intersection(normalized):
            raise ValueError("A skill path cannot be both a file and a directory")
        object.__setattr__(self, "name", frontmatter["name"])
        object.__setattr__(self, "description", frontmatter["description"])
        object.__setattr__(self, "frontmatter", _freeze_json(frontmatter))
        object.__setattr__(self, "files", MappingProxyType(normalized))
        object.__setattr__(self, "directories", frozenset(dirs))

    @classmethod
    def from_files(
        cls,
        files: Mapping[str, str | bytes],
        *,
        directories: Iterable[str] = (),
    ) -> Skill:
        """Create a snapshot from a mapping of relative paths to bytes or text."""
        instance = object.__new__(cls)
        normalized = {
            _validate_relative_path(path): content.encode("utf-8")
            if isinstance(content, str)
            else bytes(content)
            for path, content in files.items()
        }
        instance._initialize(normalized, directories)
        return instance

    @classmethod
    def from_directory(cls, path: str | os.PathLike[str]) -> Skill:
        """Read a skill directory once and return an immutable snapshot."""
        root = Path(path).resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"Skill path is not a directory: {root}")
        files: dict[str, bytes] = {}
        directories: list[str] = []
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise ValueError(
                    f"Skill directories cannot contain symlinks: {candidate}"
                )
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_dir():
                directories.append(relative)
            elif candidate.is_file():
                files[relative] = candidate.read_bytes()
            else:
                raise ValueError(f"Unsupported file in skill directory: {candidate}")
        if root.name != _parse_frontmatter(files.get("SKILL.md", b"")).get("name"):
            raise ValueError(
                "Skill directory name must match SKILL.md frontmatter name"
            )
        return cls.from_files(files, directories=directories)

    @classmethod
    def from_package(cls, package: str, path: str) -> Skill:
        """Load one skill snapshot from package resources."""
        from importlib import resources

        clean_path = _validate_relative_path(path)
        root = resources.files(package).joinpath(*clean_path.split("/"))
        if not root.is_dir():
            raise ValueError(f"Skill package path is not a directory: {package}:{path}")
        files: dict[str, bytes] = {}
        directories: list[str] = []

        def visit(directory: Any, prefix: str = "") -> None:
            for entry in directory.iterdir():
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_dir():
                    directories.append(relative)
                    visit(entry, relative)
                elif entry.is_file():
                    files[relative] = entry.read_bytes()

        visit(root)
        if root.name != _parse_frontmatter(files.get("SKILL.md", b"")).get("name"):
            raise ValueError(
                "Skill package directory name must match SKILL.md frontmatter name"
            )
        return cls.from_files(files, directories=directories)
