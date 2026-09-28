"""Discover skills and read content with manifest verification."""

import asyncio
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider
from fastmcp.server.skills import SkillsExtension


async def main() -> None:
    skills_dir = Path(__file__).parent / "sample_skills"
    server = FastMCP("Skills Server")
    server.add_provider(SkillsDirectoryProvider(skills_dir))
    server.add_extension(SkillsExtension())

    async with Client(server) as client:
        for skill in await client.list_skills():
            print(f"{skill.name}: {skill.description}")
            instructions = await skill.read()
            print(instructions.text[:500])

            if skill.resources != "dynamic":
                for file in skill.resources:
                    if file.uri.endswith("/reference.md"):
                        reference = await skill.read("reference.md")
                        print(reference.text[:500])


if __name__ == "__main__":
    asyncio.run(main())
