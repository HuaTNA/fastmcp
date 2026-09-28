"""Publish a directory of skills over the MCP Skills extension."""

from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider
from fastmcp.server.skills import SkillsExtension

mcp = FastMCP("Skills Server")
skills = SkillsDirectoryProvider(Path(__file__).parent / "sample_skills")
mcp.add_provider(skills)
mcp.add_extension(SkillsExtension())


if __name__ == "__main__":
    mcp.run()
