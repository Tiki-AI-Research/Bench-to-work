"""Agent adapter selection. Adapters are imported lazily so unused SDKs are not required."""

import sys
from pathlib import Path

from ..factories import load_factory


def make_agent(config: dict, project: Path):
    """Instantiate the configured thin adapter without plugin auto-discovery or class hierarchies."""
    options = dict(config)
    kind = options.pop("type")
    if kind == "python":
        return load_factory(options.pop("factory"))(
            project=project.resolve(), **options
        )
    if kind == "pi":
        from .pi import PiAgent

        return PiAgent(project.resolve(), **options)
    if kind == "codex":
        from .codex import CodexAgent

        if "executable" in options:
            options["executable"] = expand_command([options["executable"]], project)[0]
        return CodexAgent(**options)
    if kind == "openai_compatible":
        from .openai_model import OpenAICompatibleModel

        return OpenAICompatibleModel(**options)
    if kind == "command":
        from .command import CommandAgent

        options["command"] = expand_command(options["command"], project)
        return CommandAgent(**options)
    raise ValueError(f"Unknown agent adapter type: {kind}")


def expand_command(command: list[str], project: Path) -> list[str]:
    """Expand explicit {python} and {project} placeholders without treating arbitrary text as file paths."""
    result = []
    for argument in command:
        if argument == "{python}":
            result.append(sys.executable)
        elif argument.startswith("{project}/"):
            parts = argument.removeprefix("{project}/").split("/")
            result.append(str(project.resolve().joinpath(*parts)))
        else:
            result.append(argument.replace("{project}", str(project.resolve())))
    return result
