"""Formatting helpers for Skills CLI output."""

from typing import Any

from rich.console import Console
from rich.markup import escape

from fastmcp.client.skills import RemoteSkill

console = Console()


def skills_to_json(skills: list[RemoteSkill]) -> list[dict[str, Any]]:
    return [
        {
            "uri": skill.uri,
            "name": skill.name,
            "description": skill.description,
            "origin": skill.origin,
            "resources": skill.files,
        }
        for skill in skills
    ]


def print_skills(skills: list[RemoteSkill]) -> None:
    console.print(f"[bold]Skills ({len(skills)})[/bold]\n")
    if not skills:
        console.print("  [dim]No skills found.[/dim]")
    for skill in skills:
        console.print(f"  [cyan]{escape(skill.name)}[/cyan] [{escape(skill.uri)}]")
        console.print(f"    {escape(skill.description)}")
    console.print()
