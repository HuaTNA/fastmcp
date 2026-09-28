# MCP Skills

This example serves skills from `sample_skills/` using the MCP Skills extension and reads them from a FastMCP client with manifest verification.

Run the server:

```bash
uv run python examples/skills/server.py
```

Or run the in-memory client example:

```bash
uv run python examples/skills/client.py
```

`Skill` is the storage-neutral authoring object. Use `Skill.from_directory()` for one disk snapshot, or `SkillsDirectoryProvider` to load a whole directory tree. `SkillsDirectoryProvider.reload()` replaces its snapshot explicitly. Servers opt into the Skills methods with `mcp.add_extension(SkillsExtension())`.

Clients use `list_skills()` and `get_skill(uri)`. A `RemoteSkill` reads files by relative path and checks each response against the server's manifest before returning its bytes. The protocol only distributes content; host applications decide whether and how to trust or activate it.
